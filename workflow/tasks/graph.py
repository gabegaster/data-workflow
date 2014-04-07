import sys
import os
import time
import csv
import collections
import datetime
import glob
from distutils.util import strtobool

from ..exceptions import InvalidTaskDefinition, NonUniqueTask, ShellError
from .. import colors
from .. import shell
from .. import resources
from .. import logger
from .task import Task


class TaskGraph(object):
    """Simple graph implementation of a list of task nodes"""

    # relative location of various storage locations
    internals_path = ".workflow"
    state_path = os.path.join(internals_path, "state.csv")
    duration_path = os.path.join(internals_path, "duration.csv")
    log_path = os.path.join(internals_path, "workflow.log")
    archive_dir = os.path.join(internals_path, "archive")

    def __init__(self, config_path, task_kwargs_list):
        self.task_list = []
        self.task_dict = {}

        # store paths once for all tasks and make sure the base
        # directory exists
        self.config_path = config_path
        self.root_directory = os.path.dirname(config_path)
        directory = os.path.dirname(self.abs_state_path)
        if not os.path.exists(directory):
            os.makedirs(directory)
        if not os.path.exists(self.abs_archive_dir):
            os.makedirs(self.abs_archive_dir)

        # Store the resources in a dictionary, keyed by name where the
        # values are resource instances
        self.resource_dict = {}

        # store the time that this task takes
        self.task_durations = {}

        # instantiate the logger instance for this workflow
        self.logger = logger.configure(self)

        # the success status is used for managing notification emails
        # in an intelligent way
        self.successful = False

        # add tasks and load all dependencies between tasks
        for task_kwargs in task_kwargs_list:
            task = Task(self, **task_kwargs)
        self._dereference_depends_aliases()
        self._link_dependencies()
        self._load_state()

    def iter_graph(self, tasks=None, downstream=True):
        """Iterate over graph with breadth-first search of task dependencies,
        starting from `tasks` or the set of tasks that do not depend
        on anything.
        http://en.wikipedia.org/wiki/Breadth-first_search
        """
        if downstream:
            tasks = tasks or self.get_source_tasks()
            popmethod = 'popleft'
            updownstream = 'downstream_tasks'
        else:
            tasks = tasks or self.get_sink_tasks()
            popmethod = 'pop'
            updownstream = 'upstream_tasks'
        horizon = collections.deque(tasks)
        done, horizon_set = set(), set(tasks)
        task_order = []
        popmethod = getattr(horizon, popmethod)
        while horizon:
            task = popmethod()
            horizon_set.discard(task)
            done.add(task)
            task_order.append(task)
            updownset = getattr(task, updownstream)
            for task in updownset.difference(done):
                if task not in horizon_set:
                    horizon.append(task)
                    horizon_set.add(task)
        return task_order

    def get_source_tasks(self):
        """Get the set of tasks that do not depend on anything else.
        """
        source_tasks = set()
        for task in self.task_list:
            if not task.upstream_tasks:
                source_tasks.add(task)
        return source_tasks

    def get_sink_tasks(self):
        """Get the set of tasks that do not have any dependencies.
        """
        sink_tasks = set()
        for task in self.task_list:
            if not task.downstream_tasks:
                sink_tasks.add(task)
        return sink_tasks

    def get_out_of_sync_tasks(self):
        out_of_sync_tasks = []
        for task in self.iter_graph():
            if not task.is_pseudotask() and not task.in_sync():
                out_of_sync_tasks.append(task)
        return out_of_sync_tasks

    def get_task_ids(self):
        """Get the list of all task ids"""
        return [task.id for task in self.task_list]

    def add(self, task):
        """Connect the task to this TaskGraph instance. This stores the task
        in the TaskGraph.task_list and puts it in the
        TaskGraph.task_dict, keyed by task.creates and task.alias (if
        it exists).
        """
        self.task_list.append(task)
        if task.alias is not None:
            if task.alias in self.task_dict:
                raise NonUniqueTask(
                    "task `alias` '%s' is not unique" % task.alias
                )
            self.task_dict[task.alias] = task
        if task.creates in self.task_dict:
            raise NonUniqueTask(
                "task `creates` '%s' is not unique" % task.creates
            )
        self.task_dict[task.creates] = task

    def subgraph_needed_for(self, task_ids):
        """Find the subgraph of all dependencies to run these tasks"""
        if not task_ids:
            return self

        # instantiate a new graph instance from the original data from
        # the tasks of self
        tasks = []
        for task_id in task_ids:
            tasks.extend(self.get_tasks(task_id))

        tasks_kwargs_list = [task.yaml_data for task in
                             self.iter_graph(tasks, downstream=False)]
        subgraph = TaskGraph(self.config_path, tasks_kwargs_list)
        return subgraph

    def get_tasks(self, task_id_or_tag):
        # get the task associated to this id or to this tag; defer to id.
        task = self.task_dict.get(task_id_or_tag)
        if task is None:
            tasks = []
            for task in self.task_list:
                if task.has_tag(task_id_or_tag):
                    tasks.append(task)
        else:
            return [task]
        return self.tag_dict.get(task)


    def _dereference_alias_helper(self, name):
        if name is None:
            return None
        for task in self.task_list:
            if task.alias == name:
                return task.creates

    def _dereference_depends_aliases(self):
        """This converts every alias used in a depends statement into the
        corresponding `creates` element in that task declaration.
        """
        for task in self.task_list:
            if isinstance(task.depends, (list, tuple)):
                for i, d in enumerate(task.depends):
                    dd = self._dereference_alias_helper(d)
                    if dd is not None:
                        task.depends[i] = dd
            else:
                dd = self._dereference_alias_helper(task.depends)
                if dd is not None:
                    task.depends = dd

    def _link_dependency_helper(self, task, dependency):
        if dependency is not None:
            dependent_task = self.task_dict.get(dependency, None)

            # if dependent_task is None, make sure it exists on the
            # filesystem otherwise this Task is not properly defined
            if dependent_task is None:
                filename = os.path.join(self.root_directory, dependency)
                if not os.path.exists(filename):
                    raise InvalidTaskDefinition(
                        "Unknown `depends` declaration '%s'" % dependency
                    )
                return

            # now add the task dependency
            task.add_task_dependency(dependent_task)

    def _link_dependencies(self):
        """Iterate over all tasks and make connections between tasks based on
        their dependencies.
        """
        for task in self.task_list:

            # instantiate the resources associated with this task here
            # to make sure we can resolve aliases if they exist.
            task.depends_resources = resources.get_or_create(
                self, task.depends_list
            )
            task.creates_resources = resources.get_or_create(
                self, task.creates_list
            )

            # omit creates resources from pseudotasks. this is
            # getting sloppy. should probably do this within a task?
            if task.is_pseudotask():
                task.creates_resources = []
                del self.resource_dict[task.creates]

            # link up the dependencies
            for dependency in task.depends_list:
                self._link_dependency_helper(task, dependency)

    def get_user_clean_confirmation(self, task_list=None,
                                    include_internals=False):
        """This method gets user confirmation about cleaning up the workflow"""
        self.logger.info(colors.red(
            "Please confirm that you want to delete the following files:"
        ))
        time.sleep(0.5)
        task_list = task_list or self.task_list
        if include_internals:
            self.logger.info(green(self.internals_path))
        for task in task_list:
            if not task.is_pseudotask():
                self.logger.info(task.creates_message())
        yesno = raw_input(colors.red("Delete aforementioned files? [Y/n] "))
        if yesno == '':
            yesno = 'y'
        return strtobool(yesno)

    def clean(self, task_list=None, include_internals=False):
        """Remove appropriate internal files managed by workflow as well as
        any resulting files created by the specified `task_list`.
        """
        if os.path.exists(self.abs_state_path) and task_list is None:
            os.remove(self.abs_state_path)
        if include_internals:
            shell.run(self.root_directory, "rm -rf %s" % self.internals_path)
            self.logger.info(
                "removed %s" % colors.green(self.internals_path)
            )
        task_list = task_list or self.task_list
        for task in task_list:
            task.clean()

    def duration_string(self, duration):
        if duration < 10 * 60:  # 10 minutes
            return "%.2f" % (duration) + " s"
        elif duration < 2 * 60 * 60:  # 2 hours
            return "%.2f" % (duration / 60) + " m"
        elif duration < 2 * 60 * 60 * 24:  # 2 days
            return "%.2f" % (duration / 60 / 60) + " h"
        else:
            return "%.2f" % (duration / 60 / 60 / 24) + " d"

    def duration_message(self, tasks, color=colors.blue):
        if len(tasks) == 0:
            return "No tasks are out of sync in this workflow (%s)" % (
                os.path.relpath(self.config_path, os.getcwd())
            )
        min_duration = 0.0
        for task in tasks:
            min_duration += self.task_durations.get(task.id, 0.0)
        max_duration, n_unknown, n_tasks = 0.0, 0, 0
        for task in self.iter_graph(tasks):
            if not task.is_pseudotask():
                n_tasks += 1
                try:
                    max_duration += self.task_durations[task.id]
                except KeyError:
                    n_unknown += 1
        msg = ''
        if n_unknown > 0:
            msg += "%d new tasks with unknown durations.\n" % (
                n_unknown,
            )
        msg += "The remaining %d-%d tasks need to be executed,\n" % (
            len(tasks),
            n_tasks,
        )
        if max_duration == min_duration == 0.0:
            msg += "which will take an indeterminate amount of time."
        elif max_duration == min_duration:
            msg += "which will take approximately %s." % (
                self.duration_string(min_duration),
            )
        else:
            msg += "which will take between %s and %s." % (
                self.duration_string(min_duration),
                self.duration_string(max_duration),
            )
        if color:
            msg = color(msg)
        return msg

    def _run_helper(self, starting_tasks, do_run_func, mock_run):
        """This is a convenience method that is used to slightly modify the
        behavior of running a workflow depending on the circumstances.
        """
        self.logger.info(self.duration_message(starting_tasks))
        for task in self.iter_graph(starting_tasks):
            if do_run_func(task):
                if mock_run:
                    task.mock_run()
                else:
                    try:
                        task.timed_run()
                    except (KeyboardInterrupt, ShellError), e:
                        self.save_state(
                            override_resource_states={task.name: ''},
                        )
                        sys.exit(getattr(e, 'exit_code', 1))
        if not mock_run:
            self.save_state()

    def run_all(self, mock_run=False):
        """Execute all tasks in the workflow, regardless of whether they are
        in sync or not.
        """
        starting_tasks = list(task for task in self.iter_graph())

        def do_run_func(task):
            return not task.is_pseudotask()

        self._run_helper(starting_tasks, do_run_func, mock_run)

    def run_all_out_of_sync(self, mock_run=False):
        """Execute all tasks in the workflow that are out of sync at runtime.
        """
        starting_tasks = self.get_out_of_sync_tasks()

        def do_run_func(task):
            return not task.is_pseudotask() and not task.in_sync()

        self._run_helper(starting_tasks, do_run_func, mock_run)

    @property
    def abs_state_path(self):
        """Convenience property for accessing state storage location"""
        return os.path.join(self.root_directory, self.state_path)

    @property
    def abs_duration_path(self):
        """Convenience property for accessing duration storage location"""
        return os.path.join(self.root_directory, self.duration_path)

    @property
    def abs_log_path(self):
        """Convenience property for accessing log storage location"""
        return os.path.join(self.root_directory, self.log_path)

    @property
    def abs_archive_dir(self):
        """Convenience property for accessing the archive location"""
        return os.path.join(self.root_directory, self.archive_dir)

    def read_from_storage(self, storage_location):
        dictionary = {}
        if os.path.exists(storage_location):
            with open(storage_location) as stream:
                reader = csv.reader(stream)
                for row in reader:
                    dictionary[row[0]] = row[1]
        return dictionary

    def write_to_storage(self, dictionary, storage_location):
        with open(storage_location, 'w') as stream:
            writer = csv.writer(stream)
            for item in dictionary.iteritems():
                writer.writerow(item)

    def get_state_from_storage(self, resource):
        if os.path.exists(self.abs_state_path):
            with open(self.abs_state_path) as stream:
                reader = csv.reader(stream)
                for row in reader:
                    if row[0] == resource:
                        return row[1]

    def _load_state(self):
        """Load the states of all resources (files, databases, etc). If the
        state file hasn't been stored yet, nothing happens. This also
        loads the duration statistics on this task.
        """
        self.task_durations.update(
            self.read_from_storage(self.abs_duration_path)
        )

        # typecast the task_durations
        for task_id, duration in self.task_durations.iteritems():
            self.task_durations[task_id] = float(duration)

    def save_state(self, override_resource_states=None):
        """Save the states of all resources (files, databases, etc). If the
        state file hasn't been stored yet, it creates a new one. Can
        optionally pass override_resource_states to set the states of
        particular elements, which can be useful for handling keyboard
        interrupts, for example.
        """

        # read all of the old storage states first, then over write
        # the old states with the current states before writing to a
        # CSV. this is important for situations where a subgraph is
        # selected to run
        after_resource_states = self.read_from_storage(self.abs_state_path)
        for name, resource in self.resource_dict.iteritems():
            after_resource_states[name] = resource.get_current_state()

        # if override states are provided, update the resources
        # accordingly
        if isinstance(override_resource_states, dict):
            after_resource_states.update(override_resource_states)

        self.write_to_storage(after_resource_states, self.abs_state_path)
        self.write_to_storage(self.task_durations, self.abs_duration_path)

    def write_archive(self, exclude_internals=False):
        """Method to backup the current workflow
        """

        # for now, create archives based on the date.
        #
        # TODO: would it be better to specify by hg/git hash id? Doing
        # dates for now to make it easy to identify a good default
        # archive to restore in self.restore_archive (the last one)
        now = datetime.datetime.now()
        archive_name = os.path.join(
            self.abs_archive_dir,
            "%s.tar.bz2" % now.strftime("%Y%m%d%H%M%S"),
        )

        # get the set of all filenames that should be archived based
        # on the current workflow specification
        all_filenames = set([os.path.basename(self.config_path)])
        if not exclude_internals:
            all_filenames.update(set([
                self.state_path,
                self.duration_path,
                self.log_path,
            ]))
        for task in self.task_list:
            all_filenames.update(task.get_all_filenames())

        # create the archive. filenames are ordered here so that the
        # corresponding archive will have a consistent md5 hash (which is
        # used in functional tests).
        command = "tar cjf %s %s" % (
            archive_name,
            ' '.join(sorted(all_filenames)),
        )
        self.logger.info(colors.bold_white(command))
        shell.run(self.root_directory, command)

    def restore_archive(self, archive):
        """Method to restore a previous archived workflow specified in
        `archive`. The archive path should be relative to the root of
        the project.
        """
        archive_name = os.path.join(self.root_directory, archive)
        command = "tar xjf %s" % archive_name
        self.logger.info(colors.bold_white(command))
        shell.run(self.root_directory, command)

    def get_available_archives(self):
        """Method to list all of the available archives"""
        available_archives = self.get_abs_available_archives()
        return [os.path.relpath(a, self.root_directory)
                for a in available_archives]

    def get_abs_available_archives(self):
        """Method to list all of the available archives"""
        available_archives = glob.glob(os.path.join(self.abs_archive_dir, '*'))
        available_archives.sort()
        return available_archives
