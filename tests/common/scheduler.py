#!/usr/bin/env python
# Copyright (c) 2012 Cloudera, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# The WorkloadExecutor class encapsulates the execution of a workload. A workload is
# defined as a set of queries for a given  data set, scale factor and a specific test
# vector. It treats a workload an the unit of parallelism.

import logging
import os

from collections import defaultdict, deque
from copy import deepcopy
from random import shuffle
from sys import exit
from threading import Lock, Thread, Event

logging.basicConfig(level=logging.INFO, format='%(name)s %(threadName)s: %(message)s')
LOG = logging.getLogger('scheduler')
LOG.setLevel(level=logging.DEBUG)


class Scheduler(object):
  """Schedules the submission of workloads across one of more clients.

  A workload execution expects the following arguments:
  query_executors: A list of initialized query executor objects.
  shuffle: Change the order of execution of queries in a workload. By default, the queries
           are executed sorted by query name.
  num_clients: The degree of parallelism.
  impalads: A list of impalads to connect to. Ignored when the executor is hive.
  """
  def __init__(self, **kwargs):
    self.query_executors = kwargs.get('query_executors')
    self.shuffle = kwargs.get('shuffle', False)
    self.iterations = kwargs.get('iterations', 1)
    self.query_iterations = kwargs.get('query_iterations', 1)
    self.impalads = kwargs.get('impalads')
    self.num_clients = kwargs.get('num_clients', 1)
    self.__exit = Event()
    self.__results = list()
    self.__result_dict_lock = Lock()
    self.__thread_name = "[%s " % self.query_executors[0].query.db + "Thread %d]"
    self.__threads = []
    self.__create_workload_threads()

  @property
  def results(self):
    """Return execution results."""
    return self.__results

  def __create_workload_threads(self):
    """Create workload threads.

    Each workload thread is analogus to a client name, and is identified by a unique ID,
    the workload that's being run and the table formats it's being run on."""
    for thread_num in xrange(self.num_clients):
      thread = Thread(target=self.__run_queries, args=[thread_num],
          name=self.__thread_name % thread_num)
      thread.daemon = True
      self.__threads.append(thread)

  def __get_next_impalad(self):
    """Maintains a rotating list of impalads"""
    self.impalads.rotate(-1)
    return self.impalads[-1]

  def __run_queries(self, thread_num):
    """Runs the list of query executors"""
    # each thread gets its own copy of query_executors
    query_executors = deepcopy(sorted(self.query_executors, key=lambda x: x.query.name))
    for j in xrange(self.iterations):
      # Randomize the order of execution for each iteration if specified.
      if self.shuffle: shuffle(query_executors)
      results = defaultdict(list)
      workload_time_sec = 0
      for query_executor in query_executors:
        query_name = query_executor.query.name
        LOG.info("Running Query: %s" % query_name)
        for i in xrange(self.query_iterations):
          if self.__exit.isSet():
            LOG.error("Another thread failed, exiting.")
            exit(1)
          try:
            query_executor.prepare(self.__get_next_impalad())
            query_executor.execute()
          # QueryExecutor only throws an exception if the query fails and abort_on_error
          # is set to True. If abort_on_error is False, then the exception is logged on
          # the console and execution moves on to the next query.
          except Exception as e:
            LOG.error("Query %s Failed: %s" % (query_name, str(e)))
            self.__exit.set()
          finally:
            LOG.info("%s query iteration %d finished in %.2f seconds" % (query_name, i+1,
              query_executor.result.time_taken))
            result = query_executor.result
            result.client_name = thread_num + 1
            self.__results.append(result)
          workload_time_sec += query_executor.result.time_taken
      if self.query_iterations == 1:
        LOG.info("Workload iteration %d finished in %s seconds" % (j+1, workload_time_sec))

  def run(self):
    """Run the query pipelines concurrently"""
    for thread_num, t in enumerate(self.__threads):
      LOG.info("Starting %s" % self.__thread_name % thread_num)
      t.start()
    for thread_num,t in enumerate(self.__threads):
      t.join()
      LOG.info("Finished %s" % self.__thread_name % thread_num)