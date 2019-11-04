#!/usr/bin/env python3
import functools
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Tuple

import argparse
import cmd2.plugin
import textwrap
from cmd2 import clipboard
from cmd2.argparse_custom import Cmd2ArgumentParser, CompletionItem
from colorama import Fore, Style
from watchdog.events import FileSystemEventHandler, FileSystemEvent, FileCreatedEvent
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch
from pygments import highlight, lexers, formatters

import katana.util
from katana.manager import Manager
from katana.monitor import JsonMonitor
from katana.repl import ctf
from katana.target import Target
from katana.unit import Unit
from katana.repl.ctf import CTFProvider, Challenge, User


class MonitoringEventHandler(FileSystemEventHandler):
    """ Receives events from watchdog for newly created files to queue """

    def __init__(self, repl: "katana.repl.Repl", *args, **kwargs):
        super(MonitoringEventHandler, self).__init__(*args, **kwargs)

        # Save the manager
        self.repl = repl

    def on_created(self, event: FileSystemEvent):
        """ Called when a new file is created """

        # We only care about files
        if not isinstance(event, FileCreatedEvent):
            return

        # Queue the event
        self.repl.manager.queue_target(event.src_path)

        # Notify the user
        with self.repl.terminal_lock:
            self.repl.async_alert(
                f"[{Fore.GREEN}!{Style.RESET_ALL}] "
                f"new target queued: {event.src_path}"
            )


class ReplMonitor(JsonMonitor):
    """ A monitor which will save important information needed to run
    the Repl katana shell. """

    def __init__(self):
        super(ReplMonitor, self).__init__()

        # The repl will assign this for us
        self.repl: Repl = None

    def on_flag(self, manager: Manager, unit: Unit, flag: str):

        # Ignore duplicate flags
        if len([f for f in self.flags if f[1] == flag]) > 0:
            return

        super(ReplMonitor, self).on_flag(manager, unit, flag)

        chain = []

        # Build chain in reverse direction
        link = unit
        while link is not None:
            chain.append(link)
            link = link.target.parent

        # Reverse the chain
        chain = chain[::-1]

        # First entry is special
        log_entry = (
            f"{Fore.MAGENTA}{chain[0]}{Style.RESET_ALL}("
            f"{Fore.RED}{chain[0].target}{Style.RESET_ALL}) - "
            f"{Fore.GREEN}completed{Style.RESET_ALL}!\n"
        )

        # Print the chain
        for n in range(1, len(chain)):
            log_entry += (
                f" {' '*n}{Fore.MAGENTA}{chain[n]}{Style.RESET_ALL}("
                f"{Fore.RED}{chain[n].target}{Style.RESET_ALL}) "
                f"{Fore.YELLOW}➜ {Style.RESET_ALL}\n"
            )
        log_entry += (
            f" {' ' * len(chain)}{Fore.GREEN}{Style.BRIGHT}{flag}{Style.RESET_ALL} - "
            f"(copied)"
        )

        if (
            "ctf" in self.repl.manager
            and "auto-submit" in self.repl.manager["ctf"]
            and self.repl.manager["ctf"]["auto-submit"]
        ):
            if unit.origin.is_url:
                u = unit.origin.url_pieces.group("uri").decode("utf-8").split("/")[-1]
            else:
                u = unit.origin.hash.hexdigest()

            if u in self.repl.ctf_targets:
                with self.repl.terminal_lock:
                    result = self.repl.ctf_provider.submit(
                        self.repl.ctf_targets[u][0], flag
                    )
                    if result:
                        log_entry += (
                            f"\n\n[{Fore.GREEN}+{Style.RESET_ALL}] ctf: "
                            f"{Fore.GREEN}correct{Style.RESET_ALL} flag for challenge {self.repl.ctf_targets[u][0].title}\n"
                        )
                    else:
                        log_entry += (
                            f"\n\n[{Fore.RED}-{Style.RESET_ALL}] ctf: "
                            f"{Fore.RED}incorrect{Style.RESET_ALL} flag for challenge {self.repl.ctf_targets[u][0].title}\n"
                        )

        # Put the flag on the clipboard
        clipboard.write_to_paste_buffer(flag)

        # Notify the user
        with self.repl.terminal_lock:
            self.repl.async_alert(log_entry)

    def on_exception(
        self, manager: katana.manager.Manager, unit: katana.unit.Unit, exc: Exception
    ) -> None:
        super(ReplMonitor, self).on_exception(manager, unit, exc)

        # Notify the user
        with self.repl.terminal_lock:
            self.repl.pexcept(exc)


def get_target_choices(repl, uncomplete=False) -> List[CompletionItem]:
    """
    Get available targets for command completion

    :param repl: The Repl object
    :return: List of completion object referring to queued targets
    """
    repl: Repl

    # Grab root targets
    targets = [t for t in repl.manager.targets if t.parent is None]

    # Filter by uncompleted units
    if uncomplete:
        targets = [t for t in targets if not t.completed]

    result = [
        CompletionItem(t.hash.hexdigest(), katana.util.ellipsize(repr(t), 40))
        for t in targets
    ]

    return result


def get_monitor_choices(repl: "katana.repl.Repl") -> List[CompletionItem]:
    """
    Get available monitors for command completion
    
    :param repl: The Repl object
    :return: List of completion objects referring to monitored directories
    """

    return [d for d in repl.directories]


def get_challenge_choices(repl: "katana.repl.Repl") -> List[CompletionItem]:
    """
    Get a list of completion items for the possible challenges
    :param repl: a katana Repl object
    :return: list completion items
    """

    if repl.ctf_provider is None:
        return []

    return [
        CompletionItem(c.ident, f"{c.title} - {c.value} points")
        for c in repl.ctf_provider.challenges
    ]


class Repl(cmd2.Cmd):
    """ A simple Katana REPL implemented using the cmd2 module.
    
    You should instantiate the manager prior to creating this object. It will
    then allow the user to modify configuration, load configuration files, and
    queue targets, however you are free to do this prior to creating the Repl.
    
    The manager _must_ be created using a ReplMonitor or subclass thereof! Further,
    you should not call `manager.start()` prior to creating this object. It will
    call `manager.start()` prior to execution of the main command loop. This is
    to ensure that the we can register the Monitor with our Repl object for
    bidirectional communication.
    """

    def __init__(self, manager: Manager):
        super(Repl, self).__init__()

        # Ensure we are using the correct monitor
        if not isinstance(manager.monitor, ReplMonitor):
            raise RuntimeError("Repl expects a subclass of ReplMonitor!")

        # Save a manager reference
        self.manager = manager

        # Ensure the monitor knows we exist
        self.manager.monitor.repl = self

        # Display full tracebacks for errors/exceptions
        self.debug = True

        # CTF provider details
        self.ctf_provider: CTFProvider = None
        self.ctf_targets: Dict[str, Tuple[Challenge, Target]] = {}

        # Create a filesystem monitor
        self.fseventhandler = MonitoringEventHandler(self)
        self.observer = Observer()
        self.directories: Dict[str, ObservedWatch] = {}

        # Start the observer
        self.observer.start()

        # Register hook to update prompt
        self.register_cmdfinalization_hook(self.finalization_hook)

        # Start the manager
        self.manager.start()

        # Update the prompt
        self.update_prompt()

    def finalization_hook(
        self, data: cmd2.plugin.CommandFinalizationData
    ) -> cmd2.plugin.CommandFinalizationData:
        """ Updated dynamic prompt """
        # Update the prompt
        self.update_prompt()
        self.poutput("")
        # Maintain exit status
        return data

    def update_prompt(self):
        """ Updates the prompt with the current state """

        # build a dynamic state
        if self.manager.barrier.n_waiting == len(self.manager.threads):
            state = f"{Fore.YELLOW}waiting{Style.RESET_ALL}"
        else:
            state = f"{Fore.GREEN}running{Style.RESET_ALL}"

        # update the prompt
        self.prompt = (
            f"{Fore.CYAN}katana{Style.RESET_ALL} - {state} - "
            f"{Fore.BLUE}{self.manager.work.qsize()} units queued{Style.RESET_ALL} "
            f"\n{Fore.GREEN}➜ {Style.RESET_ALL}"
        )

    status_parser = Cmd2ArgumentParser(
        description="Display status message for all running threads"
    )
    status_parser.add_argument(
        "--flags",
        "-f",
        action="store_true",
        help="Show all flags as well as thread status",
    )

    @cmd2.with_argparser(status_parser)
    def do_status(self, args):
        for tid, status in self.manager.monitor.thread_status.items():
            unit: Unit = status[0]
            case: Any = status[1]
            if case is not None:
                self.poutput(
                    f"thread[{tid}]: {repr(unit)} -> {katana.util.ellipsize(case, 20)}"
                )
            else:
                self.poutput(f"thread[{tid}]: {repr(unit)}")

        if args.flags is not None:
            self.poutput("Flags found so far: ")
            for unit, flag in self.manager.monitor.flags:
                self.poutput(f"{repr(unit)}: {flag}")

    exit_parser = Cmd2ArgumentParser(
        description="Cleanup currently running evaluation and exit"
    )
    exit_parser.add_argument(
        "--timeout",
        "-t",
        type=float,
        help="Timeout for waiting in outstanding evaluations",
    )
    exit_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force exit prior to evaluation completion",
    )

    @cmd2.with_argparser(exit_parser)
    def do_exit(self, args: argparse.Namespace) -> bool:
        """
        Exit the katana REPL. Optionally, force current evaluation to complete immediately.
        
        :param args: argparse Namespace containing parameters
        :return: whether to exit or not (always True)
        """

        if args.force is not None and args.force:
            self.poutput(f"[{Fore.YELLOW}!{Style.RESET_ALL}] forcing thread exit")
            self.manager.abort()
        else:
            self.poutput(
                f"[{Fore.BLUE}-{Style.RESET_ALL}] waiting for thread completion (timeout={args.timeout})"
            )
            self.terminal_lock.release()
            result = self.manager.join(args.timeout)
            self.terminal_lock.acquire()
            if not result:
                self.poutput(f"[{Fore.YELLOW}!{Style.RESET_ALL}] evaluation timeout")

        self.poutput(f"[{Fore.GREEN}+{Style.RESET_ALL}] manager exited cleanly")

        return True

    @cmd2.with_argparser(exit_parser)
    def do_quit(self, args: argparse.Namespace) -> bool:
        """ Same as do_exit """
        return self.do_exit(args)

    # The main argument parser
    monitor_parser = Cmd2ArgumentParser(
        description=r"""Begin monitoring the given directory and automatically queue new targets """
        """as they are created."""
    )
    # Subparsers object to create sub-commands
    monitor_subparsers: argparse._SubParsersAction = monitor_parser.add_subparsers(
        help="Actions", required=True, dest="_action"
    )

    # `list` parser
    monitor_list_parser: Cmd2ArgumentParser = monitor_subparsers.add_parser(
        "list",
        aliases=["ls", "l"],
        help="list currently monitored directories",
        prog="monitor ls",
    )
    monitor_list_parser.set_defaults(action="list")

    # `remove` parser
    monitor_remove_parser: Cmd2ArgumentParser = monitor_subparsers.add_parser(
        "remove",
        aliases=["rm", "r"],
        help="remove a monitored directory",
        prog="monitor remove",
    )
    monitor_remove_parser.add_argument(
        "directory",
        nargs="+",
        help="The directories to stop monitoring",
        choices_method=get_monitor_choices,
    )
    monitor_remove_parser.set_defaults(action="remove")

    # `add` parser
    monitor_add_parser: Cmd2ArgumentParser = monitor_subparsers.add_parser(
        "add",
        aliases=["a"],
        help="begin monitoring a new directory",
        prog="monitor add",
    )
    monitor_add_parser.add_argument(
        "--recursive",
        "-r",
        default=False,
        action="store_true",
        help="Monitor the directory recursively",
    )
    monitor_add_parser.add_argument(
        "directory",
        nargs="+",
        help="The directories to monitor",
        completer_method=functools.partial(
            cmd2.Cmd.path_complete, path_filter=lambda path: os.path.isdir(path)
        ),
    )
    monitor_add_parser.set_defaults(action="add")

    @cmd2.with_argparser(monitor_parser)
    def do_monitor(self, args: argparse.Namespace) -> bool:
        """ Add a directory to the fs observer """

        if args.action == "add":
            for dir in args.directory:
                if not os.path.isdir(dir):
                    self.perror(f"[{Fore.RED}!{Style.RESET}] {dir}: not a directory")
                    continue
                abs_dir = os.path.realpath(os.path.abspath(dir))
                if abs_dir in self.directories:
                    self.perror(
                        f"[{Fore.RED}!{Style.RESET_ALL}] {dir}: already monitored"
                    )
                    continue
                self.directories[abs_dir] = self.observer.schedule(
                    self.fseventhandler, dir, args.recursive
                )
        elif args.action == "remove":
            # Remove currently monitored directories
            for dir in args.directory:

                # Make sure it exists
                if not os.path.isdir(dir):
                    self.perror(f"[{Fore.RED}!{Style.RESET}] {dir}: not a directory")
                    continue

                # Get the full canonical path
                dir = os.path.realpath(os.path.abspath(dir))

                # Ensure we are actually monitoring it
                if dir not in self.directories:
                    self.perror(
                        f"[{Fore.RED}!{Style.RESET}] {dir}: not being monitored"
                    )
                    continue

                # Remove it from the observer
                handle = self.directories[dir]
                del self.directories[dir]
                self.observer.unschedule(handle)

        elif args.action == "list":
            # List all monitored directories
            output = ""
            for path, handle in self.directories.items():
                if handle.is_recursive:
                    output += f"\n{handle.path} - {Fore.CYAN}recursive{Style.RESET_ALL}"
                else:
                    output += (
                        f"\n{handle.path} - {Fore.BLUE}non-recursive{Style.RESET_ALL}"
                    )
            self.poutput(output[1:])

        # Don't exit
        return False

    # Main target argument parser
    target_parser = Cmd2ArgumentParser(
        description="Add, remove, and view queued targets"
    )
    target_subparsers: argparse._SubParsersAction = target_parser.add_subparsers(
        help="Actions", required=True, dest="_action"
    )

    @cmd2.with_argparser(target_parser)
    def do_target(self, args: argparse.Namespace) -> bool:
        """ Add/stop/list queued targets """
        actions = {
            "add": self._target_add,
            "stop": self._target_stop,
            "list": self._target_list,
            "solution": self._target_solution,
            "view": self._target_view,
        }
        actions[args.action](args)
        return False

    # View target results
    target_view_parser: Cmd2ArgumentParser = target_subparsers.add_parser(
        "view", help="View results from the given target"
    )
    target_view_parser.add_argument(
        "target",
        help="The target to view",
        choices_method=functools.partial(get_target_choices, uncomplete=False),
    )
    target_view_parser.set_defaults(action="view")

    def _target_view(self, args: argparse.Namespace) -> None:
        """ View a target results """

        target = None
        for t in self.manager.targets:
            if t.hash.hexdigest() == args.target:
                target = t
                break
        else:
            self.perror(
                f"[{Fore.RED}-{Style.RESET_ALL}] {args.target}: target does not exist"
            )
            return

        results = self.manager.monitor.build_results(target=target)
        if len(results) == 0:
            self.poutput(
                f"[{Fore.YELLOW}!{Style.RESET_ALL}] {args.target}: no results found"
            )
            return

        pretty_json = json.dumps(
            results, sort_keys=True, indent=4, separators=(",", ": ")
        )

        self.ppaged(
            highlight(pretty_json, lexers.JsonLexer(), formatters.TerminalFormatter())
        )

    # Add a new target
    target_add_parser: Cmd2ArgumentParser = target_subparsers.add_parser(
        "add", aliases=["a"], help="Add a new target for processing"
    )
    target_add_parser.add_argument(
        "target",
        nargs="+",
        help="the target to evaluate",
        completer_method=cmd2.Cmd.path_complete,
    )
    target_add_parser.set_defaults(action="add")

    def _target_add(self, args: argparse.Namespace) -> None:
        """ Add a new target for evaluation """

        for target in args.target:
            self.poutput(f"[{Fore.GREEN}+{Style.RESET_ALL}] {target}: queuing target")
            self.manager.queue_target(target)

    # Stop a running target
    target_stop_parser: Cmd2ArgumentParser = target_subparsers.add_parser(
        "stop", aliases=["s", "cancel", "c"], help="Stop evaluation of a queued target"
    )
    target_stop_parser.add_argument(
        "target",
        nargs="+",
        help="the target id (hash) to stop",
        choices_method=functools.partial(get_target_choices, uncomplete=True),
    )
    target_stop_parser.set_defaults(action="stop")

    def _target_stop(self, args: argparse.Namespace) -> None:
        """ Stop processing the given target """

        # Stop each target
        for target in args.target:
            # Look for a matching hash
            for other in self.manager.targets:
                if other.hash.hexdigest() == target:
                    # Notify the user if it's already completed
                    if other.completed:
                        self.poutput(
                            f"[{Fore.YELLOW}!{Style.RESET_ALL}] {target}: already completed"
                        )
                    else:
                        other.completed = True

    # List queued targets
    target_list_parser: Cmd2ArgumentParser = target_subparsers.add_parser(
        "list", aliases=["ls", "l", "show"], help="List all queued targets"
    )
    target_list_parser.add_argument(
        "--completed",
        "-c",
        action="store_const",
        const="completed",
        dest="which",
        help="Display only completed targets",
    )
    target_list_parser.add_argument(
        "--running",
        "-r",
        action="store_const",
        const="running",
        dest="which",
        help="Display only running targets",
    )
    target_list_parser.add_argument(
        "--all",
        "-a",
        action="store_const",
        const="all",
        dest="which",
        help="Display all targets (running/completed)",
    )
    target_list_parser.add_argument(
        "--flags",
        "-f",
        action="store_const",
        const="flags",
        dest="which",
        help="Display only targets with flags",
    )
    target_list_parser.set_defaults(action="list")

    def _target_list(self, args: argparse.Namespace) -> None:
        """
        Display a list of completed and/or running targets that have been queued.
        
        :param args: The argparse Namespace
        :return: None
        """

        targets: List[Target] = []

        if args.which is None or args.which == "all":
            # In this context, we mean root targets only
            targets = [t for t in self.manager.targets if t.parent is None]
        elif args.which == "completed":
            targets = [
                t for t in self.manager.targets if t.completed and t.parent is None
            ]
        elif args.which == "running":
            targets = [
                t for t in self.manager.targets if not t.completed and t.parent is None
            ]
        elif args.which == "flags":
            targets = [f[0].origin for f in self.manager.monitor.flags]

        output = ""

        for target in targets:
            # Grab the status
            if target.completed:
                status = f"{Fore.GREEN}completed{Style.RESET_ALL}"
            else:
                status = f"{Fore.YELLOW}running{Style.RESET_ALL}"

            # Grab first flag
            flags = [f[1] for f in self.manager.monitor.flags if f[0].origin == target]

            # Build initial output
            output += (
                f"\n{Fore.RED}{target}{Style.RESET_ALL} - {status}\n"
                f" hash: {Fore.CYAN}{target.hash.hexdigest()}{Style.RESET_ALL}\n"
            )

            # Add flags if there are any
            output += "\n".join(
                f" flag: {Fore.GREEN}{Style.BRIGHT}{f}{Style.RESET_ALL}" for f in flags
            )

        # Print the list
        if len(output) > 0:
            self.poutput(output)

    # View target solutions (chain of units producing flags)
    target_solution_parser: Cmd2ArgumentParser = target_subparsers.add_parser(
        "solution", aliases=["flags"], help="List solution chains for all found flags"
    )
    target_solution_parser.add_argument(
        "--raw",
        "-r",
        action="store_true",
        help="Match the specified target by the target upstream string vice the hash",
    )
    target_solution_parser.add_argument(
        "target",
        help="The target hash or upstream (if --raw is specified)",
        choices_method=get_target_choices,
    )
    target_solution_parser.set_defaults(action="solution")

    def _target_solution(self, args: argparse.Namespace) -> None:
        """
        Display all found solutions for this target.
        
        :param args: argparse Namespace object with parsed parameters
        :return:
        """

        # Match based on target hash
        flags = [
            f
            for f in self.manager.monitor.flags
            if f[0].origin.hash.hexdigest() == args.target
        ]

        # Ensure we found at least one target
        if len(flags) == 0:
            self.perror(f"[{Fore.RED}-{Style.RESET_ALL}] {args.target}: no flags found")
            return
        elif len(flags) > 1:
            # We found more than one, assume the first matching
            self.poutput(
                f"[{Fore.YELLOW}!{Style.RESET_ALL}] {args.target}: selecting "
                f"{Fore.RED}{flags[0][0].origin}{Style.RESET_ALL}"
            )

        # Either the first or only flag
        flag: Tuple[Unit, str] = flags[0]

        # Generate the solution output
        log_entry = self.generate_solution(flag)

        # Print the entry
        self.poutput(log_entry)

    def generate_solution(self, flag):

        # The chain of units upward
        chain = []

        # Build chain in reverse direction
        link = flag[0]
        while link is not None:
            chain.append(link)
            link = link.target.parent

        # Reverse the chain
        chain = chain[::-1]

        # First entry is special
        log_entry = (
            f"{Fore.MAGENTA}{chain[0]}{Style.RESET_ALL}("
            f"{Fore.RED}{chain[0].target}{Style.RESET_ALL})\n"
        )

        # Print the chain
        for n in range(1, len(chain)):
            log_entry += (
                f" {' '*n}{Fore.MAGENTA}{chain[n]}{Style.RESET_ALL}("
                f"{Fore.RED}{chain[n].target}{Style.RESET_ALL}) "
                f"{Fore.YELLOW}➜ {Style.RESET_ALL}\n"
            )
        log_entry += (
            f" {' ' * len(chain)}{Fore.GREEN}{Style.BRIGHT}{flag[1]}{Style.RESET_ALL} - "
            f"(copied)"
        )

        return log_entry

    set_parser = Cmd2ArgumentParser(
        description=r"""Set or retreive a katana runtime parameter. Parameters may be specified as """
        r"""SECTION[NAME] or simply NAME. If no section is specified, 'DEFAULT' is assumed. """
        r"""If no value is specified, the value will be printed. If no parameter or value is """
        r"""specified, then all sections are displayed. """
    )
    set_parser.add_argument(
        "--section", "-s", action="store_true", help="Show entire section contents"
    )
    set_parser.add_argument(
        "--reset", "-r", action="store_true", help="remove/reset a parameter"
    )
    set_parser.add_argument(
        "parameter", nargs=argparse.OPTIONAL, help="The parameter to modify"
    )
    set_parser.add_argument("value", nargs=argparse.OPTIONAL, help="The value to set")

    @cmd2.with_argparser(set_parser)
    def do_set(self, args: argparse.Namespace):
        """ Set a runtime parameter """
        pattern = r"([a-zA-Z_\-0-9]*)\[([a-zA-Z_\-0-9]*)\]"

        if args.parameter is not None:
            # Check if we are specifying section[parameter]
            match = re.match(pattern, args.parameter)
            if match is not None:
                # Grab each piece
                section, name = match[1], match[2]
            else:
                # Otherwise, assume default
                section = "DEFAULT"
                name = args.parameter

            # Ensure the section exists
            if section not in self.manager and not args.value:
                self.perror(f"{section}: no such configuration section")
                return False

        if args.value:
            # Ensure the section exists
            if section not in self.manager:
                self.manager[section] = {}
            # Set the value
            self.manager[section][name] = args.value
        elif args.parameter is None:
            # Display the entire configuration
            for section in ["DEFAULT"] + self.manager.sections():
                # Print section
                self.poutput(f"[{section}]")

                # Print each item in the section
                for name in self.manager[section]:
                    if section == "DEFAULT" or name not in self.manager["DEFAULT"]:
                        self.poutput(f"  {name} = {self.manager[section][name]}")

        elif args.section is None:
            if args.reset:
                self.poutput(f"removing {section}[{name}]")
                self.manager.remove_option(section, name)
            else:
                # Display a single value within a section
                self.poutput(f"[{section}]")
                self.poutput(f"{name} = {self.manager[section][name]}")
        else:
            # Display an entire section either specifying section[name] or section alone
            if match is None:
                # We specified section alone, but it was captured in name above
                section = name
            # Ensure this exists (may have slipped past above check in the name variable)
            if section not in self.manager:
                self.perror(f"{section}: no such configuration section")
            else:
                # Print the whole section
                self.poutput(f"[{section}]")
                for name in self.manager[section]:
                    self.poutput(f"{name} = {self.manager[section][name]}")

        # All done! Don't exit.
        return False

    config_parser = Cmd2ArgumentParser(
        description="Load supplemental configuration from a file"
    )
    config_parser.add_argument(
        "file",
        help="Configuration file",
        nargs="+",
        completer_method=cmd2.Cmd.path_complete,
    )

    @cmd2.with_argparser(config_parser)
    def do_config(self, args: argparse.Namespace) -> bool:
        """
        Load a supplemental configuration file
        
        :param args: argparse Namespace with parameters
        :return: False
        """

        self.manager.read(args.file)

        return False

    # CTF integration
    ctf_parser = Cmd2ArgumentParser(
        description="Interact with a CTF instance to easily view challenges and queue targets"
    )
    ctf_subparsers: argparse._SubParsersAction = ctf_parser.add_subparsers(
        help="Commands", required=True, dest="_action"
    )

    @cmd2.with_argparser(ctf_parser)
    def do_ctf(self, args: argparse.Namespace) -> bool:
        """
        Interact with an integrated CTF platform through a provider
        
        :param args: argparse Namespace containing subcommand and arguments
        :return: False
        """

        # Build the CTF Parser if needed
        if self.ctf_provider is None:
            if (
                "ctf" not in self.manager
                or "provider" not in self.manager["ctf"]
                or "url" not in self.manager["ctf"]
                or "username" not in self.manager["ctf"]
                or "password" not in self.manager["ctf"]
            ):
                # We need these parameters
                self.perror(
                    f"[{Fore.RED}-{Style.RESET_ALL} ctf: provider, url, username, and password are required."
                )
                return False
            else:
                try:
                    # Try to authenticate
                    self.ctf_provider = ctf.get_provider(
                        self.manager["ctf"]["provider"],
                        self.manager["ctf"]["url"],
                        self.manager["ctf"]["username"],
                        self.manager["ctf"]["password"],
                    )
                except ctf.AuthenticationError as e:
                    # Bad parameters
                    self.perror(
                        f"[{Fore.RED}-{Style.RESET_ALL}] ctf: provider authentication failed: {str(e)}"
                    )
                    return False

        # Call sub-command handler
        actions = {
            "list": self._ctf_list,
            "show": self._ctf_show,
            "queue": self._ctf_queue,
            "scoreboard": self._ctf_scoreboard,
            "submit": self._ctf_submit,
            "status": self._ctf_status,
            "bracket": self._ctf_bracket,
        }
        actions[args.action](args)

        return False

    # Submit a solution to a CTF challenge
    ctf_submit_parser: argparse.ArgumentParser = ctf_subparsers.add_parser(
        "submit", help="Manually submit a challenge flag"
    )
    ctf_submit_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force submission even if challenge is already solved",
    )
    ctf_submit_parser.add_argument(
        "challenge_id",
        type=str,
        help="Challenge ID to submit to",
        choices_method=get_challenge_choices,
    )
    ctf_submit_parser.add_argument("flag", type=str, help="Flag to submit")
    ctf_submit_parser.set_defaults(action="submit")

    def _ctf_submit(self, args: argparse.Namespace) -> None:
        """
        Manually submit a flag
        :param args: arguments
        :return: None
        """

        try:
            challenge = self.ctf_provider.get_challenge(args.challenge_id)
        except RuntimeError as e:
            self.perror(f"[{Fore.RED}-{Style.RESET_ALL}] ctf: invalid challenge id")
            return

        if challenge.solved and not args.force:
            self.pwarning(
                f"[{Fore.YELLOW}!{Style.RESET_ALL}] ctf: challenge already solved"
            )
            return

        if self.ctf_provider.submit(challenge, args.flag)[0]:
            self.poutput(
                f"[{Fore.GREEN}+{Style.RESET_ALL}] ctf: "
                f"{Fore.GREEN}correct{Style.RESET_ALL} flag for {challenge.title}"
            )
        else:
            self.poutput(
                f"[{Fore.YELLOW}!{Style.RESET_ALL}] ctf: "
                f"{Fore.RED}incorrect{Style.RESET_ALL} flag for {challenge.title}"
            )

    # `ctf list` parser
    ctf_list_parser: argparse.ArgumentParser = ctf_subparsers.add_parser(
        "list", help="List all challenges on the CTFd server"
    )
    ctf_list_parser.add_argument(
        "--force", "-f", action="store_true", help="Force challenge cache refresh"
    )
    ctf_list_parser.set_defaults(action="list")

    def _ctf_list(self, args: argparse.Namespace) -> None:
        """
        List all avaiable challenge IDs
        
        :param args: argparse Namespace object with parameters
        :return: None
        """

        # Grab challenges
        challenges: List[Challenge] = list(self.ctf_provider.challenges)

        max_value = max([c.value for c in challenges])
        value_width = len(str(max_value))
        id_width = max([len(c.ident) for c in challenges]) + 2
        title_width = max([len(c.title) for c in challenges]) + 2

        # Header line
        output = [
            f"{Style.BRIGHT}{'ID':<{id_width}}"
            f"{'Title':<{title_width}}"
            f"Points{Style.RESET_ALL}"
        ]

        for c in sorted(challenges, key=lambda c: c.solved):

            # Calculate point color based on percent of max points
            value_percent = c.value / max_value
            if value_percent > 0.66:
                value_color = Fore.RED
            elif value_percent > 0.33:
                value_color = Fore.YELLOW
            else:
                value_color = Fore.GREEN

            # Calculate name style based on challenge completion
            name_style = ""
            if c.solved:
                name_style = f"\x1b[9m{Style.DIM}"

            output.append(
                f"{Fore.CYAN}{c.ident:<{id_width}}{Style.RESET_ALL}"
                f"{name_style}{c.title+Style.RESET_ALL:<{title_width+len(Style.RESET_ALL)}}"
                f"{value_color}{c.value}{Style.RESET_ALL}"
            )

        # Print paged if needed
        self.ppaged("\n".join(output))

    # `ctf queue` parser
    ctf_queue_parser: argparse.ArgumentParser = ctf_subparsers.add_parser(
        "queue", help="Queue a challenge for evaluation"
    )
    ctf_queue_parser.add_argument(
        "--description",
        "-d",
        action="store_true",
        help="Queue description for analysis as well as challenge files",
    )
    ctf_queue_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Queue challenge even if it is already solved.",
    )
    ctf_queue_parser.add_argument(
        "challenge_id",
        type=str,
        help="Challenge ID to queue",
        choices_method=get_challenge_choices,
    )
    ctf_queue_parser.set_defaults(action="queue")

    def _ctf_queue(self, args: argparse.Namespace) -> None:
        """
        Queue a challenge for evaluation
        
        :param args:
        :return: None
        """

        # Grab the challenge
        try:
            challenge = self.ctf_provider.get_challenge(args.challenge_id)
        except Exception as e:
            self.perror(f"[{Fore.YELLOW}!{Style.RESET_ALL}] ctf: invalid challenge id")
            return

        # Don't queue solved challenges
        if challenge.solved and not args.force:
            self.poutput(
                f"[{Fore.GREEN}+{Style.RESET_ALL}] ctf: challenge already solved"
            )
            return

        # Queue attached files
        for file, url in challenge.files.items():
            self.poutput(f"[{Fore.GREEN}+{Style.RESET_ALL}] ctf: queuing {file}")
            self.ctf_targets[file] = [challenge, None]
            self.ctf_targets[file][1] = self.manager.queue_target(bytes(url, "utf-8"))

        # Queue description
        if args.description:
            self.poutput(
                f"[{Fore.GREEN}+{Style.RESET_ALL}] ctf: queueing challenge {args.challenge_id} description"
            )
            key = hashlib.md5(challenge.description.encode("utf-8")).hexdigest()
            self.ctf_targets[key] = [challenge, None]
            self.ctf_targets[key][1] = self.manager.queue_target(
                bytes(challenge.description, "utf-8")
            )

        return

    # Get a list of brackets
    ctf_bracket_parser: argparse.ArgumentParser = ctf_subparsers.add_parser(
        "bracket", help="Display the scoreboard brackets"
    )
    ctf_bracket_parser.set_defaults(action="bracket")

    def _ctf_bracket(self, args: argparse.Namespace) -> None:
        """
        Show all scoreboard brackets
        :param args: argparse Namespace holding parameters
        :return: None
        """
        brackets = self.ctf_provider.brackets

        if len(brackets) == 0:
            return

        bracket_width = max([len(bracket.name) for bracket in brackets]) + 2

        output = [f" {Style.BRIGHT}{'Bracket':<{bracket_width}}ID{Style.RESET_ALL}"]
        for bracket in brackets:
            output.append(f" {bracket.name:<{bracket_width}}{bracket.ident}")

        self.ppaged("\n".join(output))

    # `ctf scoreboard`
    ctf_scoreboard_parser: argparse.ArgumentParser = ctf_subparsers.add_parser(
        "scoreboard", aliases=["board", "scores"], help="Show the scoreboard"
    )
    ctf_scoreboard_parser.add_argument(
        "--count", "-c", type=int, default=10, help="How many users to show"
    )
    ctf_scoreboard_parser.add_argument(
        "--bracket", "-b", default=None, help="Scoreboard bracket to show"
    )
    ctf_scoreboard_parser.add_argument(
        "--top",
        "-t",
        action="store_true",
        help="Display only the top users on the scoreboard",
    )
    ctf_scoreboard_parser.set_defaults(action="scoreboard")

    def _ctf_scoreboard(self, args: argparse.Namespace) -> None:
        """
        Show the top N users on the scoreboard.
        
        :param args: argparse Namespace holding parameters
        :return: None
        """

        me = self.ctf_provider.me

        if args.top:
            if args.bracket is None:
                bracket = None
            else:
                bracket = [
                    b for b in self.ctf_provider.brackets if b.name == args.bracket
                ]
                if len(bracket) == 0:
                    self.perror(
                        f"[{Fore.RED}-{Style.RESET_ALL} ctf: invalid bracket: {args.bracket}"
                    )
                    return
                bracket = bracket[0]

            scoreboard = self.ctf_provider.scoreboard(count=args.count, bracket=bracket)
        else:
            scoreboard = self.ctf_provider.scoreboard(localize=me, count=args.count)

        if len(scoreboard) == 0:
            self.poutput(
                f"[{Fore.YELLOW}!{Style.RESET_ALL}] ctf: no scoreboard available"
            )
            return

        # Get width of user column
        user_width = max([len(x.team) for p, x in scoreboard.items()]) + 2
        pos_width = max([len(str(i)) for i in scoreboard]) + 2

        # Build the table
        output = [
            f"{Style.BRIGHT}{' '*pos_width}{'Name':<{user_width}}Score{Style.RESET_ALL}"
        ]
        for pos, user in scoreboard.items():
            if user.name == me.name or (me.team is not None and user.team == me.team):
                color = Fore.MAGENTA
            else:
                color = Style.DIM
            output.append(
                f"{str(pos)+'.':<{pos_width}}"
                f"{color}{user.name:<{user_width}}{Style.RESET_ALL}"
                f"{user.score}"
            )
        output = "\n".join(output)

        # Print it
        self.ppaged(output)

    # `ctf show`
    ctf_show_parser: argparse.ArgumentParser = ctf_subparsers.add_parser(
        "show", aliases=["details", "info"], help="Show challenge details"
    )
    ctf_show_parser.add_argument(
        "--urls",
        "-u",
        action="store_true",
        help="Show full file URLs vice their file names",
    )
    ctf_show_parser.add_argument(
        "challenge_id",
        type=str,
        help="Challenge to view",
        choices_method=get_challenge_choices,
    )
    ctf_show_parser.set_defaults(action="show")

    def _ctf_show(self, args: argparse.Namespace) -> None:
        """
        Queue a challenge for evaluation
        
        :param args:
        :return:
        """

        try:
            challenge = self.ctf_provider.get_challenge(args.challenge_id)
        except RuntimeError as e:
            self.perror(f"[{Fore.RED}-{Style.RESET_ALL}] ctf: invalid challenge id")
            return

        # Grab all challenges
        challenges: List[Challenge] = list(self.ctf_provider.challenges)

        # Get the maximum value for challenges
        max_value = max([c.value for c in challenges])

        description = " " + "\n ".join(
            textwrap.wrap(challenge.description, 79, break_long_words=False)
        )

        # Dynamic colors for points based on percent of max challenge value
        value_percent = challenge.value / max_value
        if value_percent > 0.66:
            value_color = Fore.RED
        elif value_percent > 0.33:
            value_color = Fore.YELLOW
        else:
            value_color = Fore.GREEN

        output = (
            f"{Fore.MAGENTA}{challenge.title}{Style.RESET_ALL} - "
            f"{value_color}{challenge.value} points{Style.RESET_ALL} - "
            f"{Fore.RED+'not ' if not challenge.solved else Fore.GREEN}solved{Style.RESET_ALL}\n"
            f"\n"
            f"{description}"
        )

        flags = []

        # Check if the description was queued. Include flags if found
        key = hashlib.md5(challenge.description.encode("utf-8")).hexdigest()
        if (
            key in self.ctf_targets
            and self.ctf_targets[key][1] is not None
            and self.ctf_targets[key][1].hash.hexdigest() in self.manager.monitor.flags
        ):
            flags.append(
                self.manager.monitor.flags[self.ctf_targets[key][1].hash.hexdigest()]
            )

        # Add files as well
        if len(challenge.files) > 0:
            # Array of file names/URLs/paths
            files = []

            # Build file array
            for f, url in challenge.files.items():
                files.append(f"  - {url if args.urls else f}")

                # Check if it's queued
                if f in self.ctf_targets and self.ctf_targets[f][1] is not None:
                    file_flags = [
                        flag
                        for flag in self.manager.monitor.flags
                        if flag[0].origin == self.ctf_targets[f][1]
                    ]
                    flags += file_flags

            # Append output string
            output += f"\n\n {Fore.CYAN}Files:\n"
            output += "\n".join(files)

        if len(flags) > 0:
            solutions = "\n".join(self.generate_solution(f) for f in flags)
            solutions = textwrap.indent(solutions, "  ")
            output += f"\n\n Solutions:\n{solutions}"

        output += "\n"

        self.poutput(output)

    ctf_status_parser: argparse.ArgumentParser = ctf_subparsers.add_parser(
        "status", help="Show current user/team status"
    )
    ctf_status_parser.set_defaults(action="status")

    def _ctf_status(self, args: argparse.Namespace) -> None:
        """
        Display the current user, team, and score/position
        
        :param args: argparse Namespace with arguments
        :return: None
        """

        me: User = self.ctf_provider.me

        output = (
            f"{Fore.MAGENTA}{me.name}{Style.RESET_ALL} - "
            f"{Fore.CYAN}{me.team if me.team is not None else 'No Team'}{Style.RESET_ALL} - "
            f"{Fore.GREEN}{me.score} points{Style.RESET_ALL}\n"
        )

        # Grab the scoreboard
        scoreboard = self.ctf_provider.scoreboard(localize=me, count=10)
        if len(scoreboard):
            output += f"\n" f"{'':<5}{Style.BRIGHT}{'Name':<20}{Style.RESET_ALL}\n"
            board_output = []
            for pos, user in scoreboard.items():
                if (user.name is not None and user.name == me.name) or (
                    user.name is None and user.team == me.team
                ):
                    color = Fore.MAGENTA
                else:
                    color = ""
                board_output.append(
                    f"{str(pos)+'.':<5}{color}{user.name:<20}{Style.RESET_ALL}"
                )
            output += "\n".join(board_output) + "\n"

        self.ppaged(output)