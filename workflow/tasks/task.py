import os
import time
import StringIO
import copy

import jinja2

from ..exceptions import InvalidTaskDefinition
from .. import colors
from .. import shell
from .. import resources


def _cast_as_list(obj):
    if isinstance(obj, (list, tuple)):
        return obj
    elif obj is None:
        return []
    elif isinstance(obj, (str, unicode)):
        return [obj]
    else:
        raise TypeError("unexpected type passed to _cast_as_list")


class Task(resources.base.BaseResource):

    def __init__(self, graph, creates=None, depends=None, alias=None,
                 command=None, **kwargs):
        self.graph = graph
        self._creates = creates
        self._depends = depends
        self._command = command
        self._alias = alias
        self._kwargs = copy.deepcopy(kwargs)

        # quick type checking to make sure the tasks in the
        # configuration file are valid
        if self._creates is None:
            raise InvalidTaskDefinition(
                "every task must define a `creates`"
            )

        # remember other attributes of this Task for rendering
        # purposes below
        self.attrs = kwargs

        # render the creates and depends templates as necessary. this
        # is to address issue #33
        # https://github.com/deanmalmgren/data-workflow/issues/33
        self.creates = self.render_template(self._creates)
        self.depends = self.render_template(self._depends)
        self.alias = self.render_template(self._alias)

        # update the attrs to reflect any changes from the template
        # rendering from global variables
        self.attrs.update({
            'creates': self.creates,
            'depends': self.depends,
            'alias': self.alias,
        })

        # save the original command strings in _command for checking
        # the state of this command and render the jinja template for
        # the command
        self.command = self.render_command_template()

        # add this task to the task graph
        self.graph.add(self)

        # this is used to store resources that are associated with
        # this task. This is set up in TaskGraph._link_dependencies
        self.depends_resources = []
        self.creates_resources = []

        # create some data structures for storing the set of tasks on
        # which this task depends (upstream_tasks) on what depends on
        # it (downstream_tasks)
        self.downstream_tasks = set()
        self.upstream_tasks = set()

        # call the BaseResource.__init__ to get this to behave like an
        # resource here, too
        super(Task, self).__init__(self.graph, 'config:'+self.id)

    @property
    def creates_list(self):
        return _cast_as_list(self.creates)

    @property
    def depends_list(self):
        return _cast_as_list(self.depends)

    @property
    def command_list(self):
        return _cast_as_list(self.command)

    @property
    def yaml_data(self):
        out = copy.deepcopy(self._kwargs)
        out.update({
            "creates": self._creates,
            "depends": self._depends,
            "command": self._command,
            "alias": self._alias,
        })
        return out

    @property
    def id(self):
        """Canonical way to identify this Task"""
        return self.alias or self.creates

    @property
    def root_directory(self):
        """Easy access to the graph's root_directory, which is stored once for
        every task
        """
        return self.graph.root_directory

    def add_task_dependency(self, depends_on):
        self.upstream_tasks.add(depends_on)
        depends_on.downstream_tasks.add(self)

    def reset_task_dependencies(self):
        self.upstream_tasks.clear()
        self.downstream_tasks.clear()

    def get_all_filenames(self):
        """Identify the set of all filenames that pertain to this task
        """
        # TODO: when we allow for non-filesystem targets, this will
        # have to change to accomodate
        resources = []
        if not self.is_pseudotask():
            resources.extend(self.creates_resources)
        resources.extend(self.depends_resources)
        all_filenames = set()
        for resource in resources:
            all_filenames.add(resource.get_filename())
        return all_filenames

    def get_current_state(self):
        """Get the state of this task"""
        # write the data for this task to a stream so that we can use
        # the machinery in self.get_stream_state to calculate the
        # state
        msg = self.creates + str(self.depends) + str(self._command) + \
            str(self.alias)
        keys = self.attrs.keys()
        keys.sort()
        for k in keys:
            msg += k + str(self.attrs[k])
        return self.get_stream_state(StringIO.StringIO(msg))

    def is_pseudotask(self):
        """Check to see if this task is a pseudotask.
        """
        return self._command is None

    def in_sync(self):
        """Test whether this task is in sync with the stored state and
        needs to be executed
        """

        # if the creates doesn't exist, its not in sync and the task
        # must be executed
        if not os.path.exists(os.path.join(self.root_directory, self.creates)):
            return False

        # if this task or any of its dependencies are out of sync,
        # then this task must be executed. We use `all` here to
        # deliberately stop checking states when any resource state is
        # out of sync (this is more computationally efficient for
        # checking large file hashes)
        return self.state_in_sync() and all(
            resource.state_in_sync() for resource in self.depends_resources
        )

    def run(self, command):
        """Run the specified shell command using Fabric-like behavior"""
        return shell.run(self.root_directory, command)

    def clean_command(self):
        return "rm -rf %s" % self.creates

    def clean(self):
        """Remove the specified target"""
        if not self.is_pseudotask():
            self.run(self.clean_command())
            self.graph.logger.info("removed %s" % self.creates_message())

    def mock_run(self):
        """Mock run this task by displaying output as if it were run"""
        self.graph.logger.info(str(self))

    def timed_run(self):
        """Run the specified task from the root of the workflow"""

        # useful message about starting this task and what it is
        # called so users know how to re-call this task if they
        # notice something fishy during execution.
        self.graph.logger.info(self.creates_message())
        start_time = time.time()

        # run each command for this task
        for command in self.command_list:
            self.graph.logger.info(self.command_message(command))
            self.run(command)

        # stop the clock and alert the user to the clock time spent
        # running the task
        self.duration = time.time() - start_time
        self.graph.logger.info(self.duration_message())

        # store the duration on the graph object
        self.graph.task_durations[self.id] = self.duration

    def _render_template_helper(self, template_str):
        env = jinja2.Environment()
        template_obj = env.from_string(template_str)
        return template_obj.render(self.attrs)

    def render_template(self, template):
        """Render a `template` using self.attrs as a template context.
        """

        if template is None:
            return None

        # if template is a list, make sure to render each element of
        # the list
        if isinstance(template, (list, tuple)):
            return [self._render_template_helper(t) for t in template]
        else:
            return self._render_template_helper(template)

    def render_command_template(self):
        """Uses jinja template syntax to render the command from the other
        data specified in the YAML file
        """
        if self.is_pseudotask():
            return None
        return self.render_template(self._command)

    def duration_message(self, color=colors.blue):
        msg = "%79s" % self.graph.duration_string(self.duration)
        if color:
            msg = color(msg)
        return msg

    def creates_message(self, color=colors.green):
        msg = self.creates
        if self.alias:
            msg += " (%s)" % self.alias
        if color:
            msg = color(msg)
        return msg

    def command_message(self, command, color=colors.bold_white,
                        pre="|-> "):
        msg = pre + command
        if color:
            msg = color(msg)
        return msg

    def __repr__(self):
        return '\n'.join(
            [self.creates_message()] +
            [self.command_message(command=command) for command in self.command]
        )
