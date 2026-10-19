"""精确替换Agent旧工具提示词，保留现有图、检索、工作区与事件集成。"""
import argparse
import ast
import re
from datetime import datetime
from pathlib import Path
import shutil


def patch_agent(source):
    tree = ast.parse(source)
    agent = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Agent"), None)
    if agent is None:
        raise ValueError("没有找到Agent类，未修改文件。")
    retrieve = next((n for n in agent.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "retrieve"), None)
    if retrieve is None:
        raise ValueError("没有找到Agent.retrieve，未修改文件。")
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    def pos(line, column):
        # AST列偏移为UTF-8字节；转换为Python字符串字符偏移。
        text = source.splitlines(keepends=True)[line - 1]
        return offsets[line - 1] + len(text.encode("utf-8")[:column].decode("utf-8"))

    def span(node):
        return pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset)

    assignments = [n for n in retrieve.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "prompt" for t in n.targets)]
    if len(assignments) != 1:
        raise ValueError("prompt构造与已知版本不同，未修改文件；请提供agent.py。")
    node = assignments[0]
    edits = [(*span(node), 'prompt = (ANSWER_PROMPT + TOOL_POLICY_PROMPT + json.dumps(Answer.model_json_schema(), ensure_ascii=False))')]
    for node in retrieve.body:
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "prompt":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and "【多轮追问与引用规则】" in node.value.value:
                edits.append((*span(node), ""))
            else:
                raise ValueError("发现未知追加提示词，未修改文件；请提供agent.py以免保留冲突规则。")
    loops = [n for n in ast.walk(retrieve) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "run_tool_loop"]
    if len(loops) != 1:
        raise ValueError("run_tool_loop调用与已知版本不同，未修改文件。")
    limits = {"max_steps": 10, "max_calls": 20, "tool_timeout": 60,
              "total_timeout": 170, "final_reserve": 25}
    seen = set()
    for keyword in loops[0].keywords:
        if keyword.arg in limits:
            edits.append((*span(keyword.value), str(limits[keyword.arg])))
            seen.add(keyword.arg)
    if seen != set(limits):
        raise ValueError("工具预算参数不完整，未修改文件。")
    for start, end, value in sorted(edits, reverse=True):
        source = source[:start] + value + source[end:]
    if not re.search(r"^from \.tool_policy import TOOL_POLICY_PROMPT\s*$", source, re.M):
        source = "from .tool_policy import TOOL_POLICY_PROMPT\n" + source
    compile(source, "agent.py", "exec")
    return source


def patch_env(text):
    for key, value in {"WEB_PUBLIC_READ_ENABLED": "true", "WEB_BROWSER_MAX_READS": "5"}.items():
        pattern = rf"^[ \t]*{key}[ \t]*=.*$"
        if re.search(pattern, text, re.M):
            text = re.sub(pattern, f"{key}={value}", text, flags=re.M)
        else:
            text = text.rstrip() + f"\n{key}={value}\n"
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="通过预检后备份并修改agent.py和.env")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for filename in ("web_tools.py", "web_access.py", "browser_reader.py", "tool_policy.py", "answer_policy.py", "crawl4ai_worker.py"):
        path = root / "app" / filename
        if not path.is_file():
            parser.error(f"缺少app/{filename}，请先按安装说明放置文件。")
        compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
    agent_path, env_path = root / "app" / "agent.py", root / ".env"
    original = agent_path.read_text(encoding="utf-8-sig")
    changed = patch_agent(original)
    env = env_path.read_text(encoding="utf-8-sig") if env_path.exists() else ""
    updated_env = patch_env(env)
    print("预检通过：通用工具策略；公开读取；前5条候选；浏览器最多5次。")
    print("工具循环：最多10轮/20次调用，总预算170秒，预留25秒用于最终回答。")
    if not args.apply:
        print("未写入。执行相同命令加 --apply 应用修改。")
        return
    backup = root / ".cache" / "public_web_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup.mkdir(parents=True)
    shutil.copy2(agent_path, backup / "agent.py")
    if env_path.exists():
        shutil.copy2(env_path, backup / "env.backup")
    try:
        agent_path.write_text(changed, encoding="utf-8")
        env_path.write_text(updated_env, encoding="utf-8")
    except Exception:
        agent_path.write_text(original, encoding="utf-8")
        if env_path.exists() or env:
            env_path.write_text(env, encoding="utf-8")
        raise
    print("已应用。agent.py和.env备份目录：", backup)
    print("请重启后端；没有修改API Key、数据库、路由或前端。")


if __name__ == "__main__":
    main()
