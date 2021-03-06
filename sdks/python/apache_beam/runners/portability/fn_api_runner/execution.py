#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Set of utilities for execution of a pipeline by the FnApiRunner."""

from __future__ import absolute_import

import collections
import itertools

from typing_extensions import Protocol

from apache_beam import coders
from apache_beam.coders.coder_impl import create_InputStream
from apache_beam.coders.coder_impl import create_OutputStream
from apache_beam.portability import common_urns
from apache_beam.portability.api import beam_fn_api_pb2
from apache_beam.runners.portability.fn_api_runner.translations import only_element
from apache_beam.runners.portability.fn_api_runner.translations import split_buffer_id
from apache_beam.runners.worker import bundle_processor
from apache_beam.transforms import trigger
from apache_beam.transforms.window import GlobalWindow
from apache_beam.transforms.window import GlobalWindows
from apache_beam.utils import windowed_value


class Buffer(Protocol):
  def __iter__(self):
    # type: () -> Iterator[bytes]
    pass

  def append(self, item):
    # type: (bytes) -> None
    pass


class PartitionableBuffer(Buffer, Protocol):
  def partition(self, n):
    # type: (int) -> List[List[bytes]]
    pass


class ListBuffer(object):
  """Used to support parititioning of a list."""
  def __init__(self, coder_impl):
    self._coder_impl = coder_impl
    self._inputs = []  # type: List[bytes]
    self._grouped_output = None
    self.cleared = False

  def append(self, element):
    # type: (bytes) -> None
    if self.cleared:
      raise RuntimeError('Trying to append to a cleared ListBuffer.')
    if self._grouped_output:
      raise RuntimeError('ListBuffer append after read.')
    self._inputs.append(element)

  def partition(self, n):
    # type: (int) -> List[List[bytes]]
    if self.cleared:
      raise RuntimeError('Trying to partition a cleared ListBuffer.')
    if len(self._inputs) >= n or len(self._inputs) == 0:
      return [self._inputs[k::n] for k in range(n)]
    else:
      if not self._grouped_output:
        output_stream_list = [create_OutputStream() for _ in range(n)]
        idx = 0
        for input in self._inputs:
          input_stream = create_InputStream(input)
          while input_stream.size() > 0:
            decoded_value = self._coder_impl.decode_from_stream(
                input_stream, True)
            self._coder_impl.encode_to_stream(
                decoded_value, output_stream_list[idx], True)
            idx = (idx + 1) % n
        self._grouped_output = [[output_stream.get()]
                                for output_stream in output_stream_list]
      return self._grouped_output

  def __iter__(self):
    # type: () -> Iterator[bytes]
    if self.cleared:
      raise RuntimeError('Trying to iterate through a cleared ListBuffer.')
    return iter(self._inputs)

  def clear(self):
    # type: () -> None
    self.cleared = True
    self._inputs = []
    self._grouped_output = None


class GroupingBuffer(object):
  """Used to accumulate groupded (shuffled) results."""
  def __init__(self,
               pre_grouped_coder,  # type: coders.Coder
               post_grouped_coder,  # type: coders.Coder
               windowing
              ):
    # type: (...) -> None
    self._key_coder = pre_grouped_coder.key_coder()
    self._pre_grouped_coder = pre_grouped_coder
    self._post_grouped_coder = post_grouped_coder
    self._table = collections.defaultdict(
        list)  # type: DefaultDict[bytes, List[Any]]
    self._windowing = windowing
    self._grouped_output = None  # type: Optional[List[List[bytes]]]

  def append(self, elements_data):
    # type: (bytes) -> None
    if self._grouped_output:
      raise RuntimeError('Grouping table append after read.')
    input_stream = create_InputStream(elements_data)
    coder_impl = self._pre_grouped_coder.get_impl()
    key_coder_impl = self._key_coder.get_impl()
    # TODO(robertwb): We could optimize this even more by using a
    # window-dropping coder for the data plane.
    is_trivial_windowing = self._windowing.is_default()
    while input_stream.size() > 0:
      windowed_key_value = coder_impl.decode_from_stream(input_stream, True)
      key, value = windowed_key_value.value
      self._table[key_coder_impl.encode(key)].append(
          value if is_trivial_windowing else windowed_key_value.
          with_value(value))

  def partition(self, n):
    # type: (int) -> List[List[bytes]]

    """ It is used to partition _GroupingBuffer to N parts. Once it is
    partitioned, it would not be re-partitioned with diff N. Re-partition
    is not supported now.
    """
    if not self._grouped_output:
      if self._windowing.is_default():
        globally_window = GlobalWindows.windowed_value(
            None,
            timestamp=GlobalWindow().max_timestamp(),
            pane_info=windowed_value.PaneInfo(
                is_first=True,
                is_last=True,
                timing=windowed_value.PaneInfoTiming.ON_TIME,
                index=0,
                nonspeculative_index=0)).with_value
        windowed_key_values = lambda key, values: [
            globally_window((key, values))]
      else:
        # TODO(pabloem, BEAM-7514): Trigger driver needs access to the clock
        #   note that this only comes through if windowing is default - but what
        #   about having multiple firings on the global window.
        #   May need to revise.
        trigger_driver = trigger.create_trigger_driver(self._windowing, True)
        windowed_key_values = trigger_driver.process_entire_key
      coder_impl = self._post_grouped_coder.get_impl()
      key_coder_impl = self._key_coder.get_impl()
      self._grouped_output = [[] for _ in range(n)]
      output_stream_list = [create_OutputStream() for _ in range(n)]
      for idx, (encoded_key, windowed_values) in enumerate(self._table.items()):
        key = key_coder_impl.decode(encoded_key)
        for wkvs in windowed_key_values(key, windowed_values):
          coder_impl.encode_to_stream(wkvs, output_stream_list[idx % n], True)
      for ix, output_stream in enumerate(output_stream_list):
        self._grouped_output[ix] = [output_stream.get()]
      self._table.clear()
    return self._grouped_output

  def __iter__(self):
    # type: () -> Iterator[bytes]

    """ Since partition() returns a list of lists, add this __iter__ to return
    a list to simplify code when we need to iterate through ALL elements of
    _GroupingBuffer.
    """
    return itertools.chain(*self.partition(1))


class WindowGroupingBuffer(object):
  """Used to partition windowed side inputs."""
  def __init__(
      self,
      access_pattern,
      coder  # type: coders.WindowedValueCoder
  ):
    # type: (...) -> None
    # Here's where we would use a different type of partitioning
    # (e.g. also by key) for a different access pattern.
    if access_pattern.urn == common_urns.side_inputs.ITERABLE.urn:
      self._kv_extractor = lambda value: ('', value)
      self._key_coder = coders.SingletonCoder('')  # type: coders.Coder
      self._value_coder = coder.wrapped_value_coder
    elif access_pattern.urn == common_urns.side_inputs.MULTIMAP.urn:
      self._kv_extractor = lambda value: value
      self._key_coder = coder.wrapped_value_coder.key_coder()
      self._value_coder = (coder.wrapped_value_coder.value_coder())
    else:
      raise ValueError("Unknown access pattern: '%s'" % access_pattern.urn)
    self._windowed_value_coder = coder
    self._window_coder = coder.window_coder
    self._values_by_window = collections.defaultdict(
        list)  # type: DefaultDict[Tuple[str, BoundedWindow], List[Any]]

  def append(self, elements_data):
    # type: (bytes) -> None
    input_stream = create_InputStream(elements_data)
    while input_stream.size() > 0:
      windowed_val_coder_impl = self._windowed_value_coder.get_impl(
      )  # type: WindowedValueCoderImpl
      windowed_value = windowed_val_coder_impl.decode_from_stream(
          input_stream, True)
      key, value = self._kv_extractor(windowed_value.value)
      for window in windowed_value.windows:
        self._values_by_window[key, window].append(value)

  def encoded_items(self):
    # type: () -> Iterator[Tuple[bytes, bytes, bytes]]
    value_coder_impl = self._value_coder.get_impl()
    key_coder_impl = self._key_coder.get_impl()
    for (key, window), values in self._values_by_window.items():
      encoded_window = self._window_coder.encode(window)
      encoded_key = key_coder_impl.encode_nested(key)
      output_stream = create_OutputStream()
      for value in values:
        value_coder_impl.encode_to_stream(value, output_stream, True)
      yield encoded_key, encoded_window, output_stream.get()


class FnApiRunnerExecutionContext(object):
  """
 :var pcoll_buffers: (collections.defaultdict of str: list): Mapping of
       PCollection IDs to list that functions as buffer for the
       ``beam.PCollection``.
 """
  def __init__(self,
      worker_handler_factory,  # type: Callable[[Optional[str], int], List[WorkerHandler]]
      pipeline_components,  # type: beam_runner_api_pb2.Components
      safe_coders,
               ):
    """
    :param worker_handler_factory: A ``callable`` that takes in an environment
        id and a number of workers, and returns a list of ``WorkerHandler``s.
    :param pipeline_components:  (beam_runner_api_pb2.Components): TODO
    :param safe_coders:
    """
    self.pcoll_buffers = {}  # type: MutableMapping[bytes, PartitionableBuffer]
    self.worker_handler_factory = worker_handler_factory
    self.pipeline_components = pipeline_components
    self.safe_coders = safe_coders


class BundleContextManager(object):

  def __init__(self,
      execution_context, # type: FnApiRunnerExecutionContext
      process_bundle_descriptor,  # type: beam_fn_api_pb2.ProcessBundleDescriptor
      worker_handler,  # type: fn_runner.WorkerHandler
      p_context,  # type: pipeline_context.PipelineContext
               ):
    self.execution_context = execution_context
    self.process_bundle_descriptor = process_bundle_descriptor
    self.worker_handler = worker_handler
    self.pipeline_context = p_context

  def get_input_coder_impl(self, transform_id):
    # type: (str) -> CoderImpl
    coder_id = beam_fn_api_pb2.RemoteGrpcPort.FromString(
        self.process_bundle_descriptor.transforms[transform_id].spec.payload
    ).coder_id
    assert coder_id
    if coder_id in self.execution_context.safe_coders:
      return self.pipeline_context.coders[
          self.execution_context.safe_coders[coder_id]].get_impl()
    else:
      return self.pipeline_context.coders[coder_id].get_impl()

  def get_buffer(self, buffer_id, transform_id):
    # type: (bytes, str) -> PartitionableBuffer

    """Returns the buffer for a given (operation_type, PCollection ID).
    For grouping-typed operations, we produce a ``GroupingBuffer``. For
    others, we produce a ``ListBuffer``.
    """
    kind, name = split_buffer_id(buffer_id)
    if kind in ('materialize', 'timers'):
      if buffer_id not in self.execution_context.pcoll_buffers:
        self.execution_context.pcoll_buffers[buffer_id] = ListBuffer(
            coder_impl=self.get_input_coder_impl(transform_id))
      return self.execution_context.pcoll_buffers[buffer_id]
    elif kind == 'group':
      # This is a grouping write, create a grouping buffer if needed.
      if buffer_id not in self.execution_context.pcoll_buffers:
        original_gbk_transform = name
        transform_proto = self.execution_context.pipeline_components.transforms[
            original_gbk_transform]
        input_pcoll = only_element(list(transform_proto.inputs.values()))
        output_pcoll = only_element(list(transform_proto.outputs.values()))
        pre_gbk_coder = self.pipeline_context.coders[
            self.execution_context.safe_coders[
                self.execution_context.pipeline_components.
                pcollections[input_pcoll].coder_id]]
        post_gbk_coder = self.pipeline_context.coders[
            self.execution_context.safe_coders[
                self.execution_context.pipeline_components.
                pcollections[output_pcoll].coder_id]]
        windowing_strategy = self.pipeline_context.windowing_strategies[
            self.execution_context.pipeline_components.
            pcollections[output_pcoll].windowing_strategy_id]
        self.execution_context.pcoll_buffers[buffer_id] = GroupingBuffer(
            pre_gbk_coder, post_gbk_coder, windowing_strategy)
    else:
      # These should be the only two identifiers we produce for now,
      # but special side input writes may go here.
      raise NotImplementedError(buffer_id)
    return self.execution_context.pcoll_buffers[buffer_id]

  def input_for(self, transform_id, input_id):
    # type: (str, str) -> str
    input_pcoll = self.process_bundle_descriptor.transforms[
        transform_id].inputs[input_id]
    for read_id, proto in self.process_bundle_descriptor.transforms.items():
      if (proto.spec.urn == bundle_processor.DATA_INPUT_URN and
          input_pcoll in proto.outputs.values()):
        return read_id
    raise RuntimeError('No IO transform feeds %s' % transform_id)
