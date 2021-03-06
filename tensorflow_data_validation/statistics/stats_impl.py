# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Beam implementation of statistics generators."""

from __future__ import absolute_import
from __future__ import division

from __future__ import print_function

import apache_beam as beam
from tensorflow_data_validation import types
from tensorflow_data_validation.statistics import stats_options
from tensorflow_data_validation.statistics.generators import common_stats_generator
from tensorflow_data_validation.statistics.generators import numeric_stats_generator
from tensorflow_data_validation.statistics.generators import stats_generator
from tensorflow_data_validation.statistics.generators import string_stats_generator
from tensorflow_data_validation.statistics.generators import top_k_stats_generator
from tensorflow_data_validation.statistics.generators import top_k_uniques_combiner_stats_generator
from tensorflow_data_validation.statistics.generators import uniques_stats_generator

from tensorflow_data_validation.utils import batch_util
from tensorflow_data_validation.types_compat import List, TypeVar

from tensorflow_metadata.proto.v0 import statistics_pb2


@beam.typehints.with_input_types(types.Example)
@beam.typehints.with_output_types(statistics_pb2.DatasetFeatureStatisticsList)
class GenerateStatisticsImpl(beam.PTransform):
  """PTransform that applies a set of generators."""

  def __init__(
      self,
      options = stats_options.StatsOptions()
      ):
    self._options = options

  def expand(self, dataset):
    # Initialize a list of stats generators to run.
    stats_generators = [
        # Create common stats generator.
        common_stats_generator.CommonStatsGenerator(
            schema=self._options.schema,
            weight_feature=self._options.weight_feature,
            num_values_histogram_buckets=\
                self._options.num_values_histogram_buckets,
            epsilon=self._options.epsilon),

        # Create numeric stats generator.
        numeric_stats_generator.NumericStatsGenerator(
            schema=self._options.schema,
            weight_feature=self._options.weight_feature,
            num_histogram_buckets=self._options.num_histogram_buckets,
            num_quantiles_histogram_buckets=\
                self._options.num_quantiles_histogram_buckets,
            epsilon=self._options.epsilon),

        # Create string stats generator.
        string_stats_generator.StringStatsGenerator(
            schema=self._options.schema),

        # Create topk stats generator.
        top_k_stats_generator.TopKStatsGenerator(
            schema=self._options.schema,
            weight_feature=self._options.weight_feature,
            num_top_values=self._options.num_top_values,
            num_rank_histogram_buckets=\
                self._options.num_rank_histogram_buckets),

        # Create uniques stats generator.
        uniques_stats_generator.UniquesStatsGenerator(
            schema=self._options.schema)
    ]
    if self._options.generators is not None:
      # Add custom stats generators.
      stats_generators.extend(self._options.generators)

    # Batch the input examples.
    desired_batch_size = (None if self._options.sample_count is None else
                          self._options.sample_count)
    dataset = (dataset | 'BatchExamples' >> batch_util.BatchExamples(
        desired_batch_size=desired_batch_size))

    # If a set of whitelist features are provided, keep only those features.
    if self._options.feature_whitelist:
      dataset |= ('RemoveNonWhitelistedFeatures' >> beam.Map(
          _filter_features, feature_whitelist=self._options.feature_whitelist))

    result_protos = []
    # Iterate over the stats generators. For each generator,
    #   a) if it is a CombinerStatsGenerator, wrap it as a beam.CombineFn
    #      and run it.
    #   b) if it is a TransformStatsGenerator, wrap it as a beam.PTransform
    #      and run it.
    for generator in stats_generators:
      if isinstance(generator, stats_generator.CombinerStatsGenerator):
        result_protos.append(
            dataset
            | generator.name >> beam.CombineGlobally(
                _CombineFnWrapper(generator)))
      elif isinstance(generator, stats_generator.TransformStatsGenerator):
        result_protos.append(
            dataset
            | generator.name >> generator.ptransform)
      else:
        raise TypeError('Statistics generator must extend one of '
                        'CombinerStatsGenerator or TransformStatsGenerator, '
                        'found object of type %s' %
                        generator.__class__.__name__)

    # Each stats generator will output a PCollection of DatasetFeatureStatistics
    # protos. We now flatten the list of PCollections into a single PCollection,
    # then merge the DatasetFeatureStatistics protos in the PCollection into a
    # single DatasetFeatureStatisticsList proto.
    return (result_protos | 'FlattenFeatureStatistics' >> beam.Flatten()
            | 'MergeDatasetFeatureStatisticsProtos' >>
            beam.CombineGlobally(_merge_dataset_feature_stats_protos)
            | 'MakeDatasetFeatureStatisticsListProto' >>
            beam.Map(_make_dataset_feature_statistics_list_proto))


def _filter_features(
    batch,
    feature_whitelist):
  """Remove features that are not whitelisted.

  Args:
    batch: A dict containing the input batch of examples.
    feature_whitelist: A list of feature names to whitelist.

  Returns:
    A dict containing only the whitelisted features of the input batch.
  """
  return {
      feature_name: batch[feature_name]
      for feature_name in feature_whitelist
      if feature_name in batch
  }


def _merge_dataset_feature_stats_protos(
    stats_protos
):
  """Merge together a list of DatasetFeatureStatistics protos.

  Args:
    stats_protos: A list of DatasetFeatureStatistics protos to merge.

  Returns:
    The merged DatasetFeatureStatistics proto.
  """
  stats_per_feature = {}
  # Iterate over each DatasetFeatureStatistics proto and merge the
  # FeatureNameStatistics protos per feature.
  for stats_proto in stats_protos:
    for feature_stats_proto in stats_proto.features:
      if feature_stats_proto.name not in stats_per_feature:
        stats_per_feature[feature_stats_proto.name] = feature_stats_proto
      else:
        stats_per_feature[feature_stats_proto.name].MergeFrom(
            feature_stats_proto)

  # Create a new DatasetFeatureStatistics proto.
  result = statistics_pb2.DatasetFeatureStatistics()
  num_examples = None
  for feature_stats_proto in stats_per_feature.values():
    # Add the merged FeatureNameStatistics proto for the feature
    # into the DatasetFeatureStatistics proto.
    new_feature_stats_proto = result.features.add()
    new_feature_stats_proto.CopyFrom(feature_stats_proto)

    # Get the number of examples from one of the features that
    # has common stats.
    if num_examples is None:
      stats_type = feature_stats_proto.WhichOneof('stats')
      stats_proto = None
      if stats_type == 'num_stats':
        stats_proto = feature_stats_proto.num_stats
      else:
        stats_proto = feature_stats_proto.string_stats

      if stats_proto.HasField('common_stats'):
        num_examples = (stats_proto.common_stats.num_non_missing +
                        stats_proto.common_stats.num_missing)

  # Set the num_examples field.
  if num_examples is not None:
    result.num_examples = num_examples
  return result


def _make_dataset_feature_statistics_list_proto(
    stats_proto
):
  """Constructs a DatasetFeatureStatisticsList proto.

  Args:
    stats_proto: The input DatasetFeatureStatistics proto.

  Returns:
    The DatasetFeatureStatisticsList proto containing the input stats proto.
  """
  # Create a new DatasetFeatureStatisticsList proto.
  result = statistics_pb2.DatasetFeatureStatisticsList()

  # Add the input DatasetFeatureStatistics proto.
  dataset_stats_proto = result.datasets.add()
  dataset_stats_proto.CopyFrom(stats_proto)
  return result




@beam.typehints.with_input_types(types.ExampleBatch)
@beam.typehints.with_output_types(
    statistics_pb2.DatasetFeatureStatistics)
class _CombineFnWrapper(beam.CombineFn):
  """Class to wrap a CombinerStatsGenerator as a beam.CombineFn."""

  def __init__(
      self,
      generator):
    self._generator = generator

  def __reduce__(self):
    return _CombineFnWrapper, (self._generator,)

  def create_accumulator(self
                        ):  # pytype: disable=invalid-annotation
    return self._generator.create_accumulator()

  def add_input(self, accumulator,
                input_batch):
    return self._generator.add_input(accumulator, input_batch)

  def merge_accumulators(self, accumulators):
    return self._generator.merge_accumulators(accumulators)

  def extract_output(
      self,
      accumulator
  ):  # pytype: disable=invalid-annotation
    return self._generator.extract_output(accumulator)


def generate_statistics_in_memory(
    examples,
    options = stats_options.StatsOptions()
):
  """Generates statistics for an in-memory list of examples.

  Args:
    examples: A list of input examples.
    options: Options for generating data statistics.

  Returns:
    A DatasetFeatureStatisticsList proto.
  """

  stats_generators = [
      common_stats_generator.CommonStatsGenerator(
          schema=options.schema,
          weight_feature=options.weight_feature,
          num_values_histogram_buckets=\
            options.num_values_histogram_buckets,
          epsilon=options.epsilon),

      numeric_stats_generator.NumericStatsGenerator(
          schema=options.schema,
          weight_feature=options.weight_feature,
          num_histogram_buckets=options.num_histogram_buckets,
          num_quantiles_histogram_buckets=\
            options.num_quantiles_histogram_buckets,
          epsilon=options.epsilon),

      string_stats_generator.StringStatsGenerator(schema=options.schema),

      top_k_uniques_combiner_stats_generator.TopKUniquesCombinerStatsGenerator(
          schema=options.schema,
          weight_feature=options.weight_feature,
          num_top_values=options.num_top_values,
          num_rank_histogram_buckets=options.num_rank_histogram_buckets),
  ]

  if options.generators is not None:
    for generator in options.generators:
      if isinstance(generator, stats_generator.CombinerStatsGenerator):
        stats_generators.append(generator)
      else:
        raise TypeError('Statistics generator used in '
                        'generate_statistics_in_memory must '
                        'extend CombinerStatsGenerator, found object of type '
                        '%s.' %
                        generator.__class__.__name__)

  batch = batch_util.merge_single_batch(examples)

  # If whitelist features are provided, keep only those features.
  if options.feature_whitelist:
    batch = {
        feature_name: batch[feature_name]
        for feature_name in options.feature_whitelist
    }

  outputs = [
      generator.extract_output(
          generator.add_input(generator.create_accumulator(), batch))
      for generator in stats_generators
  ]

  return _make_dataset_feature_statistics_list_proto(
      _merge_dataset_feature_stats_protos(outputs))
