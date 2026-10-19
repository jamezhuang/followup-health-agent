"""Run step 1 only: local health-memory regression, with a saved report.

Usage from apps/api: python -m scripts.accept_health_memory
No login, real API request, application startup or database migration.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime
import uuid
import xml.etree.ElementTree as ET


TASKS = (
    "健康档案模块测试收集和通过",
    "真实数据库、后端、前端与原档案读写",
    "MD年龄及过敏史冲突、确认更新与版本追溯",
    "真实聊天说明未确认差异且不擅自更新",
    "重新上传、资料修改、切换账号与访问隔离",
    "长对话压缩、事实保留、重启恢复与输入量下降",
)


def source_hash(path):
    text = path.read_text(encoding="utf-8-sig")
    canonical = "\n".join(text.splitlines()).rstrip() + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def redactor(root):
    secrets = []
    secret_name = re.compile(r"key|token|secret|password|passwd", re.I)
    for name, value in os.environ.items():
        if secret_name.search(name) and len(value) >= 8:
            secrets.append(value)
    env_file = root / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            name, sep, value = line.partition("=")
            if sep and secret_name.search(name):
                value = value.strip().strip("\"'")
                if len(value) >= 8:
                    secrets.append(value)

    def clean(text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = str(text or "")
        for value in sorted(set(secrets), key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]{12,}", r"\1[REDACTED]", text)
        return text

    return clean


def preflight(root, baseline):
    issues = []
    for relative in baseline["required_files"]:
        path = root / relative
        if not path.is_file():
            issues.append({"code": "file_missing", "file": relative})
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
        except (SyntaxError, UnicodeError) as error:
            issues.append({"code": "source_invalid", "file": relative,
                           "line": getattr(error, "lineno", None), "type": type(error).__name__})
            continue
        if path.stem in {"health_memory", "health_memory_integration"}:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in {
                    path.stem, "app.workspace." + path.stem
                }:
                    issues.append({"code": "self_import", "file": relative, "line": node.lineno})
    return issues


def parse_junit(path):
    result = {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
              "case_names": [], "failures": []}
    if not path.is_file():
        return result
    for case in ET.parse(path).getroot().iter("testcase"):
        result["collected"] += 1
        name = case.get("name", "")
        result["case_names"].append(name)
        failed, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
        if failed is not None or error is not None:
            kind = "failed" if failed is not None else "errors"
            result[kind] += 1
            node = failed if failed is not None else error
            result["failures"].append({"name": name, "kind": kind,
                "message": node.get("message", "")[:1500], "trace": (node.text or "")[-5000:]})
        elif skipped is not None:
            result["skipped"] += 1
        else:
            result["passed"] += 1
    return result


def diagnose(log, report):
    suggestions = []
    for issue in report["preflight_issues"]:
        if issue["code"] == "self_import":
            suggestions.append("发现模块导入自身；请核对两个不同文件的内容和放置位置。")
        elif issue["code"] == "file_missing":
            suggestions.append("缺少 " + issue["file"] + "；请恢复对应文件，不创建空文件代替。")
        else:
            suggestions.append("源文件解析失败：" + issue["file"] + "；核对编码和语法。")
    checks = (
        ("partially initialized module", "发现循环导入；检查health_memory.py是否误放了integration文件内容。"),
        ("ModuleNotFoundError", "缺少模块或依赖；先查看pytest.log中的具体模块名，确认用的是apps/api的虚拟环境。"),
        ("UnicodeDecodeError", "存在编码错误；源码与测试数据读取应显式使用UTF-8。"),
        ("no tests ran", "没有执行测试；检查tests/test_health_memory.py是否为当前完整版本。"),
        ("ImportError", "测试收集时导入失败；查看pytest.log中最早的项目文件位置。"),
    )
    for marker, advice in checks:
        if marker in log:
            suggestions.append(advice)
    if report.get("timed_out"):
        suggestions.append("测试超过时间限制；检查日志是否卡在外部请求或全局fixture初始化。")
    if report.get("missing_critical_tests"):
        suggestions.append("缺少当前基线的关键测试，需核对test_health_memory.py版本。")
    if report.get("pytest_exit_code") == 0 and report["status"] != "passed":
        suggestions.append("pytest返回成功但用例数量或跳过情况不满足完整验收；核对测试汇总和基线。")
    if report["status"] == "failed" and not suggestions:
        suggestions.append("查看报告中失败用例的断言与pytest.log，先定位原因再修改对应实现。")
    return list(dict.fromkeys(suggestions))


def main(argv=None):
    parser = argparse.ArgumentParser(description="健康档案第1步验收：仅运行模块测试并保存结果")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="apps/api目录；默认由脚本位置确定")
    parser.add_argument("--timeout", type=int, default=180, help="pytest超时秒数，默认180")
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout须为正整数")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = args.project_root.resolve()
    baseline_path = Path(__file__).with_name("health_memory_baseline.json")
    if not baseline_path.is_file():
        print("缺少scripts/health_memory_baseline.json，请与脚本放在同一目录。")
        return 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8-sig"))
    stamp = datetime.now().astimezone()
    out = root / ".cache" / "health_memory_acceptance" / (stamp.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    out.mkdir(parents=True)
    clean = redactor(root)
    report = {"schema_version": 1, "created_at": stamp.isoformat(), "status": "blocked",
              "python": sys.version.split()[0], "python_executable": sys.executable,
              "scope": "step1_module_regression", "uses_real_postgresql": False,
              "uses_real_llm": False, "preflight_issues": preflight(root, baseline),
              "tests": {}, "file_checks": [], "pytest_exit_code": None, "timed_out": False}
    for item in baseline["reviewed_files"]:
        path = root / item["path"]
        try:
            actual = source_hash(path) if path.is_file() else None
        except UnicodeError:
            actual = None
        report["file_checks"].append({**item, "actual_sha256": actual, "matches_reviewed": actual == item["sha256"]})
    report["package_versions"] = {}
    for name in ("pytest", "pydantic", "SQLAlchemy", "fastapi", "pgvector"):
        try:
            report["package_versions"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            report["package_versions"][name] = None
    log = "未运行pytest：先修正preflight_issues所列文件问题。\n"
    xml_path = out / "junit.xml"
    if not report["preflight_issues"]:
        command = [sys.executable, "-m", "pytest", "tests/test_health_memory.py", "-q", "--tb=short", "-ra",
                   "-o", "addopts=", "--junitxml=" + str(xml_path)]
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            process = subprocess.run(command, cwd=root, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=args.timeout)
            report["pytest_exit_code"] = process.returncode
            log = process.stdout
        except subprocess.TimeoutExpired as error:
            report["timed_out"] = True
            log = clean(error.stdout) + "\n测试超时。\n"
        if xml_path.is_file():
            xml_path.write_text(clean(xml_path.read_text(encoding="utf-8")), encoding="utf-8")
        try:
            report["tests"] = parse_junit(xml_path)
        except ET.ParseError:
            report["junit_error"] = "报告未完整生成，请查看pytest.log。"
        names = {name.split("[", 1)[0] for name in report["tests"].get("case_names", [])}
        report["missing_critical_tests"] = sorted(set(baseline["critical_tests"]) - names)
        tests = report["tests"]
        accepted = (report["pytest_exit_code"] == 0 and tests.get("passed", 0) >= baseline["minimum_cases"]
                    and not tests.get("failed") and not tests.get("errors") and not tests.get("skipped")
                    and not report["missing_critical_tests"])
        report["status"] = "passed" if accepted else "failed"
    log = clean(log)
    report["suggestions"] = diagnose(log, report)
    report["progress"] = [{"step": index + 1, "task": task,
        "status": report["status"] if index == 0 else "not_run"} for index, task in enumerate(TASKS)]
    report["next_step"] = 2 if report["status"] == "passed" else 1
    (out / "pytest.log").write_text(log[:1_000_000], encoding="utf-8")
    (out / "result.json").write_text(clean(json.dumps(report, ensure_ascii=False, indent=2)), encoding="utf-8")
    totals = {key: value for key, value in report["tests"].items() if key not in {"case_names", "failures"}}
    lines = ["# 健康档案验收记录", "", "第1项：" + report["status"], "",
             "测试汇总：`" + json.dumps(totals, ensure_ascii=False) + "`", "",
             "本次范围为模拟模型和SQLite模块测试。第2—6项均未执行。", "",
             "## 下一步", "", "第" + str(report["next_step"]) + "项。"]
    lines.extend("\n- " + text for text in report["suggestions"])
    (out / "report.md").write_text(clean("\n".join(lines)), encoding="utf-8")
    print("第1项：" + report["status"])
    print("测试汇总：" + json.dumps({key: value for key, value in report["tests"].items()
                                      if key not in {"case_names", "failures"}}, ensure_ascii=False))
    print("报告目录：" + str(out))
    print("其他验收项：not_run；下一项：" + str(report["next_step"]))
    for advice in report["suggestions"]:
        print("排查建议：" + advice)
    if any(not row["matches_reviewed"] for row in report["file_checks"]):
        print("部分文件与已核对版本不同，差异已记录；测试结果以本次执行为准。")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
