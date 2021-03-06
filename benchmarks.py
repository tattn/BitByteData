#!/usr/bin/env python3

import argparse
import datetime
from enum import Enum, auto
import json
import math
import re
import subprocess
import sys

# TODO: At some point we should probably remove support for working with saved results without iter_count.

class StatKeeper:
    def __init__(self):
        self._regs = 0
        self._imps = 0
        self._oks = 0

    def reg(self):
        self._regs += 1

    def imp(self):
        self._imps += 1

    def ok(self):
        self._oks += 1

    def summary(self) -> str:
        total = self._oks + self._imps + self._regs
        output = "Out of all compared results:\n"
        output += "    {0}/{1} regressions\n".format(self._regs, total)
        output += "    {0}/{1} improvements\n".format(self._imps, total)
        output += "    {0}/{1} no significant changes".format(self._oks, total)
        return output

stat_keeper = StatKeeper()

class PvalueResult(Enum):
    LESS = auto()
    EQUAL = auto()
    GREATER = auto()

class PvalueStat:
    def __init__(self, result, base):
        n1 = base.iter_count
        n2 = result.iter_count
        self.df = n1 + n2 - 2 # Degrees of freedom
        pooled_sd = math.sqrt(((n1 - 1) * pow(base.sd, 2) + (n2 - 1) * pow(result.sd, 2)) / self.df)
        se = pooled_sd * math.sqrt(1 / n1 + 1 / n2)
        self.t_stat = (float(base.avg) - float(result.avg)) / se
        if self.df == 18:
            if abs(self.t_stat) > 2.101:
                # p-value < 0.05
                self.res = PvalueResult.LESS
            elif abs(self.t_stat) == 2.101:
                # p-value = 0.05
                self.res = PvalueResult.EQUAL
            else:
                # p-value > 0.05
                self.res = PvalueResult.GREATER
        else:
            self.res = None

class BenchmarkResult:
    def __init__(self, group: str, bench: str, avg: str, rsd: str, iter_count: int=None):
        self.group_name = group
        self.test_name = bench
        self.avg = avg
        self.rel_std_dev = rsd
        self.iter_count = iter_count

    def __str__(self):
        return "{test_name}   {avg} {rsd}%".format(test_name=self.test_name, avg=self.avg, rsd=self.rel_std_dev)

    @classmethod
    def from_json_dict(cls, dct: dict):
        return cls("", dct["name"], dct["avg"], dct["rel_std_dev"], dct.get("iter_count"))

    # Standard deviation
    @property
    def sd(self):
        return float(self.avg) * float(self.rel_std_dev) / 100

    def str_compare(self, base) -> str:
        output = str(self)
        output += "  |  {avg} {rsd}% | ".format(avg=base.avg, rsd=base.rel_std_dev)
        p_value_stat = PvalueStat(self, base)
        diff = (float(self.avg) / float(base.avg) - 1) * 100
        if diff > 0:
            if p_value_stat.res == PvalueResult.GREATER:
                output += "OK (p-value > 0.05)"
                stat_keeper.ok()
            elif p_value_stat.res is None:
                output += "REG +{0:.2f}% (df={1}, t-stat={2:.2f})".format(diff, p_value_stat.df, p_value_stat.t_stat)
                stat_keeper.reg()
            elif p_value_stat.res == PvalueResult.LESS:
                output += "REG +{0:.2f}% (p-value < 0.05)".format(diff)
                stat_keeper.reg()
            elif p_value_stat.res == PvalueResult.EQUAL:
                output += "REG +{0:.2f}% (p-value = 0.05)".format(diff)
                stat_keeper.reg()
            else:
                raise RuntimeError("Unknown p-value result")
        elif diff < 0:
            diff = abs(diff)
            if p_value_stat.res == PvalueResult.GREATER:
                output += "OK (p-value > 0.05)"
                stat_keeper.ok()
            elif p_value_stat.res is None:
                output += "IMP -{0:.2f}% (df={1}, t-stat={2:.2f})".format(diff, p_value_stat.df, p_value_stat.t_stat)
                stat_keeper.imp()
            elif p_value_stat.res == PvalueResult.LESS:
                output += "IMP -{0:.2f}% (p-value < 0.05)".format(diff)
                stat_keeper.imp()
            elif p_value_stat.res == PvalueResult.EQUAL:
                output += "IMP -{0:.2f}% (p-value = 0.05)".format(diff)
                stat_keeper.imp()
            else:
                raise RuntimeError("Unknown p-value result")
        else:
            output += "OK"
            stat_keeper.ok()
        return output

class BenchmarkGroup:
    def __init__(self, name):
        self.name = name
        self.results = {}

    def __str__(self):
        output = "{0}:\n".format(self.name)
        for result in self.results.values():
            output += "  {0}\n".format(result)
        return output

    def add_result(self, result: BenchmarkResult):
        self.results[result.test_name] = result

    def str_compare(self, base) -> str:
        output = "{0}: NEW | BASE\n".format(self.name)
        for result_name, result in self.results.items():
            base_result = base.results.get(result_name)
            if base_result is None:
                output += "  {0} | N/A\n".format(result)
            else:
                output += "  " + result.str_compare(base_result) + "\n"
        return output

class BenchmarkRun:
    def __init__(self, swift_ver, timestamp=None, description=None):
        self.groups = {}
        self.swift_ver = swift_ver
        self.timestamp = timestamp
        self.description = description

    def __str__(self):
        output = ""
        if self.swift_ver is not None:
            output += "{0}".format(self.swift_ver)
        if self.timestamp is not None:
            output += "Timestamp: {0}\n".format(self.timestamp)
        if self.description is not None:
            output += "Description: {0}\n".format(self.description)
        for group_name, group in self.groups.items():
            output += "\n" + group_name + ":\n"
            for result_name, result in group.results.items():
                output += "  {test_name}   {avg} {rsd}%".format(test_name=result_name, avg=result.avg,
                                                                rsd=result.rel_std_dev) + "\n"
        return output

    def new_result(self, result: BenchmarkResult):
        group = self.groups.get(result.group_name, BenchmarkGroup(result.group_name))
        group.add_result(result)
        self.groups[group.name] = group

    def str_compare(self, base, ignore_missing=False) -> str:
        output = ""
        for group_name, group in self.groups.items():
            base_group = base.groups.get(group_name)
            if base_group is None:
                output += str(group) + "\n"
                output += "warning: " + group_name + " not found in base benchmarks\n"
            else:
                output += group.str_compare(base_group) + "\n"
            if not ignore_missing and base_group is not None:
                missing_results = []
                for result in base_group.results.values():
                    if result.test_name not in group.results:
                        missing_results.append(result.test_name)
                if len(missing_results) > 0:
                    output += "warning: following results were found in base benchmarks but not in new:\n"
                    output += ", ".join(missing_results)
                    output += "\n"
        output += stat_keeper.summary() + "\n"
        return output

class BenchmarkJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, BenchmarkRun):
            run_out = []
            for group_name, group in o.groups.items():
                results_out = []
                for result in group.results.values():
                    results_out.append({"name": result.test_name, 
                                        "avg": result.avg, 
                                        "rel_std_dev": result.rel_std_dev,
                                        "iter_count": result.iter_count})
                group_out = {"group_name": group_name, "results": results_out}
                run_out.append(group_out)
            d = {"swift_ver": o.swift_ver, "timestamp": o.timestamp}
            if o.description is not None:
                d["description"] = o.description
            d["BitByteDataBenchmarks"] = run_out
            return d
        return json.JSONEncoder.default(self, o)

class BenchmarkJSONDecoder(json.JSONDecoder):
    def __init__(self, *args, **kwargs):
        json.JSONDecoder.__init__(self, object_hook=self.object_hook, *args, **kwargs)

    def object_hook(self, obj):
        if len(obj.items()) >= 3 and "name" in obj and "avg" in obj and "rel_std_dev" in obj:
            return BenchmarkResult.from_json_dict(obj)
        elif len(obj.items()) == 2 and "group_name" in obj:
            group = BenchmarkGroup(obj["group_name"])
            for result in obj["results"]:
                group.add_result(result)
            return group
        elif len(obj.items()) >= 2 and "BitByteDataBenchmarks" in obj and "swift_ver" in obj:
            run = BenchmarkRun(obj["swift_ver"], obj.get("timestamp"), obj.get("description"))
            for group in obj["BitByteDataBenchmarks"]:
                run.groups[group.name] = group
            return run
        return obj

def _group_benches(benches: list) -> dict:
    groups = {}
    for bench in benches:
        if bench.startswith("BitByteDataBenchmarks."):
            name_parts = bench[22:].split("/")
            if len(name_parts) > 2:
                print("warning: unknown benchmark naming format, skipping.")
                continue
            group = groups.get(name_parts[0], [])
            group.append(name_parts[1])
            groups[name_parts[0]] = group
        else:
            print("warning: non-benchmark test was returned by --filter, skipping.")
    return groups

def _sprun(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout, stderr=result.stderr)
    return result 

def action_run(args):
    # Output format of 'swift test' differs between macOS and Linux platforms.
    regex = ""
    if sys.platform == "darwin":
        regex = (r"Test Case '-\[BitByteDataBenchmarks\.(.+Benchmarks) (test.+)\]'.+average: (\d+.\d+), "
                 r"relative standard deviation: (\d+.\d+)\%, values: \[(.*)\]")
    elif sys.platform == "linux":
        regex = (r"Test Case '(.+Benchmarks)\.(test.+)'.+average: (\d+.\d+), "
                 r"relative standard deviation: (\d+.\d+)\%, values: \[(.*)\]")
    else:
        raise RuntimeError("Unknown platform: " + sys.platform) 
    p = re.compile(regex)
    iter_p = re.compile(r"(\d+.\d+)")# For calculating number of iterations.

    swift_command = []
    if args.toolchain is not None:
        swift_command = ["xcrun", "-toolchain", args.toolchain]
    elif args.use_5:
        swift_command = ["xcrun", "-toolchain", "org.swift.50320190830a"]
    swift_command.append("swift")

    # Loading base benchmarks if necessary.
    base = None
    if args.compare is not None:
        f_base = open(args.compare, "r")
        base = json.load(f_base, cls=BenchmarkJSONDecoder)
        f_base.close()
        print("BASE: " + args.compare)
        if base.swift_ver is not None:
            print(base.swift_ver, end="")
        if base.timestamp is not None:
            print("Timestamp: {0}".format(base.timestamp))
        if base.description is not None:
            print("Description: {0}".format(base.description))

    if args.clean:
        print("Cleaning...")
        _sprun(["rm", "-rf", ".build/"])

    print("Building...")
    _sprun(swift_command + ["build", "--build-tests", "-c", "release"])

    bench_list = _sprun(swift_command + ["test", "-c", "release", "-l", "--filter", args.filter]).stdout.decode().splitlines()
    groups = _group_benches(bench_list)
    if len(groups) == 0:
        print("No benchmarks have been found according to the specified options. Exiting...")
        return
    
    print("Benchmarking...")
    swift_ver = subprocess.run(swift_command + ["--version"], stdout=subprocess.PIPE, check=True,
                               universal_newlines=True).stdout
    run = BenchmarkRun(swift_ver, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), args.desc)

    bench_command = swift_command + ["test", "-c", "release", "--filter"]
    for group, benches in groups.items():
        base_group = None
        if base is not None:
            base_group = base.groups.get(group)
        for bench in benches:
            base_result = None
            if base_group is not None:
                base_result = base_group.results.get(bench)
            # Regex symbols are necessary to filter tests exactly according to our benchmark name.
            # Otherwise swift may run more than one benchmark.
            raw_name = "^BitByteDataBenchmarks.{0}/{1}$".format(group, bench)
            command = bench_command + [raw_name]
            output = _sprun(command).stdout.decode().splitlines()
            for line in output:
                matches = p.findall(line.rstrip())
                if len(matches) == 1 and len(matches[0]) == 5:
                    if matches[0][0] != group or matches[0][1] != bench:
                        raise RuntimeError("Seems like swift executed wrong benchmark")
                    result = BenchmarkResult(group, bench, matches[0][2], matches[0][3])
                    matches = iter_p.findall(matches[0][4])
                    result.iter_count = len(matches)
                    run.new_result(result)
                    if base_result is not None:
                        print("{0}/{1}".format(group, result.str_compare(base_result)))
                    else:
                        print("{0}/{1}".format(group, result))
    
    if base is not None:
        print(stat_keeper.summary())

    if args.save is not None:
        f = open(args.save, "w+")
        json.dump(run, f, indent=2, cls=BenchmarkJSONEncoder)
        f.close()

def action_show(args):
    f = open(args.file, "r")
    o = json.load(f, cls=BenchmarkJSONDecoder)
    f.close()
    if args.compare is not None:
        f_base = open(args.compare, "r")
        base = json.load(f_base, cls=BenchmarkJSONDecoder)
        f_base.close()
        print("BASE: " + args.compare)
        if base.swift_ver is not None:
            print(base.swift_ver, end="")
        if base.timestamp is not None:
            print("Timestamp: {0}".format(base.timestamp))
        if base.description is not None:
            print("Description: {0}".format(base.description))
        print("\nNEW: " + args.file)
        if o.swift_ver is not None:
            print(o.swift_ver, end="")
        if o.timestamp is not None:
            print("Timestamp: {0}".format(o.timestamp))
        if o.description is not None:
            print("Description: {0}".format(o.description))
        print(o.str_compare(base))
    else:
        print(o)

parser = argparse.ArgumentParser(description="A benchmarking tool for BitByteData")
subparsers = parser.add_subparsers(title="commands", help="a command to perform", metavar="CMD")

# Parser for 'run' command.
parser_run = subparsers.add_parser("run", help="run benchmarks", description="run benchmarks")
parser_run.add_argument("--filter", action="store", default="BitByteDataBenchmarks",
                        help="filter benchmarks (passed as --filter option to 'swift test')")
parser_run.add_argument("--save", action="store", metavar="FILE", help="save output in a file")
parser_run.add_argument("--compare", action="store", metavar="BASE", help="compare results with base benchmarks")
parser_run.add_argument("--desc", action="store", metavar="DESC", help="add a description to the results")
parser_run.add_argument("--no-clean", action="store_false", dest="clean", help="don't perform cleaning stage")

toolchain_option_group = parser_run.add_mutually_exclusive_group()
toolchain_option_group.add_argument("--toolchain", action="store", metavar="ID",
                                    help="use swift from the toolchain with specified identifier")
toolchain_option_group.add_argument("--5", action="store_true", dest="use_5",
                                    help=("use swift from the toolchain with 'org.swift.50320190830a' identifier (this is"
                                        " the release toolchain for Swift 5.0.3; useful when Xcode with version less than "
                                        "10.2.1 is used)"))

parser_run.set_defaults(func=action_run)

# Parser for 'show' command.
parser_show = subparsers.add_parser("show", help="print saved benchmarks results", description="print saved benchmarks results")
parser_show.add_argument("file", action="store", metavar="FILE",
                        help="file with benchmarks results in JSON format")
parser_show.add_argument("--compare", action="store", metavar="BASE", help="compare results with base benchmarks")
parser_show.set_defaults(func=action_show)

args = parser.parse_args()
args.func(args)
