import os
import pathlib
import sys
import traceback
import tempfile
import typing as T
import shutil
import dictdiffer 
import dictdiffer.utils
from ruamel.yaml import YAML

from ..autowrap.generator_data import MissingReporter
from ..cmd.header2dat import make_argparser, generate_wrapper
from ..makeplan import InputFile, makeplan, BuildTarget, CompilerInfo


class YamlUpdater:
    @classmethod
    def add_subparser(cls, parent_parser, subparsers):
        parser = subparsers.add_parser(
            "update-yaml",
            help="Updates YAML files from parsed header files",
            parents=[parent_parser],
        )
        parser.add_argument(
            "--project_file", help="The path to the pyproject.toml file", type=pathlib.Path, default=pathlib.Path("./pyproject.toml")
        )
        parser.add_argument(
            "--output_directory", help="Where the updated yaml files will be written", type=pathlib.Path, default=pathlib.Path(".")
        )
        parser.add_argument(
            "-v", "--verbose", help="Show full traceback", action="store_true"
        )

        return parser

    def run(self, args):
        try:
            self._run(args)
        except Exception as e:
            # Reading the stack trace is annoying, most of the time the exception content
            # is enough to figure out what you did wrong.
            if args.verbose:
                raise

            msg = [
                "ERROR: exception occurred when generating YAML from `pyproject.toml` config\n\n",
            ]

            msg += traceback.format_exception_only(type(e), e)
            cause = e.__context__
            while cause is not None:

                el = traceback.format_exception_only(type(cause), cause)
                el[0] = f"- caused by {el[0]}"
                msg += el

                if cause.__suppress_context__:
                    break

                cause = cause.__context__

            msg.append("\nUse -v/--verbose option for stacktrace")

            print("".join(msg), file=sys.stderr)
            sys.exit(1)

    def _run(self, args):
        project_root = args.project_file.parent.absolute()

        output_directory = args.output_directory.absolute()

        tmp_backup_dir = tempfile.TemporaryDirectory()
        backup_dir = pathlib.Path(tmp_backup_dir.name)
        shutil.copytree(project_root / "semiwrap", tmp_backup_dir.name + "/semiwrap")

        tmp_generated_dir = tempfile.TemporaryDirectory()
        generated_dir = pathlib.Path(tmp_generated_dir.name)
        os.chdir(generated_dir)

        # Problem: if another hatchling plugin sets PKG_CONFIG_PATH to include a .pc
        # file, makeplan() will fail to find it, which prevents a semiwrap program
        # from consuming those .pc files.
        #
        # We search for .pc files in the project root by default and add anything found
        # to the PKG_CONFIG_PATH to allow that to work. Probably won't hurt anything?

        pcpaths: T.Set[str] = set()
        for pcfile in project_root.glob("**/*.pc"):
            pcpaths.add(str(pcfile.parent))

        if pcpaths:
            # Add to PKG_CONFIG_PATH so that it can be resolved by other hatchling
            # plugins if desired
            pkg_config_path = os.environ.get("PKG_CONFIG_PATH")
            if pkg_config_path is not None:
                os.environ["PKG_CONFIG_PATH"] = os.pathsep.join(
                    (pkg_config_path, *pcpaths)
                )
            else:
                os.environ["PKG_CONFIG_PATH"] = os.pathsep.join(pcpaths)

        plan = makeplan(project_root, missing_yaml_ok=True)

        parsing_args = []

        for item in plan:
            if not isinstance(item, BuildTarget) or item.command != "header2dat":
                continue

            # convert args to string so we can parse it
            # .. this is weird, but less annoying than other alternatives
            #    that I can think of?
            argv = []
            for arg in item.args:
                if isinstance(arg, str):
                    argv.append(arg)
                elif isinstance(arg, InputFile):
                    argv.append(str(arg.path.absolute()))
                elif isinstance(arg, pathlib.Path):
                    argv.append(str(arg.absolute()))
                elif isinstance(arg, CompilerInfo):
                    argv += ["pcpp", "ignored", "ignored"]
                else:
                    # anything else shouldn't matter
                    argv.append("ignored")

            self.handle_header(argv)

        self.merge_data(generated_dir, backup_dir, output_directory)

    def handle_header(self, argv):
        
        sparser = make_argparser()
        sargs = sparser.parse_args(argv)

        reporter = MissingReporter()

        if sargs.cpp:
            sargs.defines.append(f"__cplusplus {sargs.cpp}")

        generate_wrapper(
            name=sargs.name,
            src_yml=sargs.src_yml,
            src_h=sargs.src_h,
            src_h_root=sargs.src_h_root,
            dst_dat=None,
            dst_depfile=None,
            include_paths=sargs.include_paths,
            compiler_flavor="pcpp",
            compiler_args=[],
            casters={},
            pp_defines=sargs.defines,
            missing_reporter=reporter,
            report_only=True,
        )

        if reporter:
            for name, report in reporter.as_yaml():
                report = f"{report}"

                name.parent.mkdir(parents=True, exist_ok=True)
                with open(name, "w") as fp:
                    fp.write(report)

    def merge_data(self, generated_directory: pathlib.Path, backup_directory: pathlib.Path, output_directory: pathlib.Path):
        generated_files = set()
        for root, _, files in os.walk(generated_directory):
            for f in files:
                generated_files.add((pathlib.Path(root) / f).relative_to(generated_directory))
                
        backup_files = set()
        for root, _, files in os.walk(backup_directory):
            for f in files:
                backup_files.add((pathlib.Path(root) / f).relative_to(backup_directory))

        common_files = backup_files.intersection(generated_files)
        deleted_files = backup_files.difference(generated_files)

        yaml_ = YAML()
        yaml_.default_flow_style = False
        yaml_.preserve_quotes = True
        yaml_.width = 4096 # Super long width to prevent line wrapping



        for f in common_files:
            generated = yaml_.load(generated_directory / f)
            original = yaml_.load(backup_directory / f)
            diffs = dictdiffer.diff(original, generated)

            additions = []

            for diff in diffs:
                action = diff[0]
                # The freshly generated version has added something. We will track a list and apply it at the end, in case that chunk is "ignored"
                if action == "add":
                    additions.append(diff)
                
                # The freshly generated version has removed something. This might be a legitimate removal, like a function being deleted, or it might be deleting some hand tweaked code that we want to keep.
                elif action == "remove":
                    removals = diff[2]
                    for removal in removals:
                        # Make a patch that contains just this one removal
                        modified_diff = [(diff[0], diff[1], [removal])]

                        # These are special hand-edited things that we want to keep. If it is not in the list, run the patch
                        if removal[0] not in ["defaults", "extra_includes", "nodelete", "template_params", "force_no_trampoline", "ignore", "templates", "typealias", "inline_code", "base_qualnames", "force_type_casters", "force_no_default_constructor", "subpackage", "is_polymorphic", "template_inline_code", "ignored_bases", "doc", "rename", "constants", "strip_prefixes"]:
                            original = dictdiffer.patch(modified_diff, original)

            # Patch all of the additions while being aware of if the file / class / etc has been marked as "ignore"
            if not original.get("defaults", {}).get("ignore", False):
                for addition in additions:
                    original_contents = (dictdiffer.utils.dot_lookup(original, addition[1]))
                    if original_contents.get("ignore", False):
                        continue
                    original = dictdiffer.patch(additions, original)
            
            output_file = output_directory / f
            output_file.parent.mkdir(parents=True, exist_ok=True)

            with open(output_file, 'w') as f:
                yaml_.dump(original, f)

        # In the event that the output directory is the same as the project directory, explicit delete files that are
        # no longer used in generation
        for f in deleted_files:
            if f.name != ".gitignore":
                file_to_delete = output_directory / f
                print(f"Deleting unused file {file_to_delete}")
                os.unlink(file_to_delete)