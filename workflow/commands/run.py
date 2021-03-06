import os
import sys

from ..exceptions import ShellError, CommandLineException
from ..notify import notify
from .base import BaseCommand, TaskIdMixin


class Command(BaseCommand, TaskIdMixin):
    help_text = "Run the task workflow."

    def inner_execute(self, task_id, force, dry_run):

        # restrict task graph as necessary for the purposes of running
        # the workflow
        if task_id is not None:
            self.task_graph = self.task_graph.subgraph_needed_for([task_id])

        # when the workflow is --force'd, this runs all
        # tasks. Otherwise, only runs tasks that are out of sync.
        if force:
            self.task_graph.run_all(mock_run=dry_run)
        else:
            self.task_graph.run_all_out_of_sync(mock_run=dry_run)

        # mark the self.task_graph as completing successfully to send the
        # correct email message
        self.task_graph.successful = True

    def execute(self, task_id=None, force=False, dry_run=False,
                notify_emails=None):
        try:
            self.inner_execute(task_id, force, dry_run)
        except CommandLineException, e:
            print(e)
            sys.exit(getattr(e, 'exit_code', 1))
        finally:
            if notify_emails:
                notify(*notify_emails)

    def add_command_line_options(self):
        self.option_parser.add_argument(
            '-f', '--force',
            action="store_true",
            help="Rerun entire workflow, regardless of task state.",
        )
        # TODO: should this be broken out as a separate `status`
        # command? This would make workflow behave more like version
        # control. Pros and cons?
        self.option_parser.add_argument(
            '-d', '--dry-run',
            action="store_true",
            help=(
                "Do not run workflow, just report which tasks would be run "
                "and how long it would take."
            ),
        )
        self.option_parser.add_argument(
            '--notify',
            type=str,
            metavar='EMAIL',
            dest="notify_emails",
            nargs=1,
            help='Specify an email address to notify on completion.',
        )
        self.add_task_id_option('Specify a particular task to run.')
