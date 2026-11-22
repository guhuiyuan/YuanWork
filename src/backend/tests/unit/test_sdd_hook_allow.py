"""SDD 权限 hook 的承诺，有人替它盯着。

这个 hook（``.claude/hooks/sdd_bash_allow.py``）是 auto 模式下的**唯一一道门**：
服务端的安全分类器不可达时，它说放行才放行。它自己不写测试，于是每改一次
它自己就多一个能钻的口子——本文件就是那些口子的看守。

它承诺的事（都在这个文件里逐条断言）：

* **不放行**危险可执行文件（rm / curl / git push / 任意 python）
* **不放行**会写文件的普通命令（cp / mv / tee / tar）—— 白名单是自动填的，
  不能让它变成后门
* **不放行**文件重定向、后台运行、命令替换
* **只读命令的路径参数必须落在项目内**——``cat ~/.ssh/id_rsa`` 这种读密钥的
  路径不能被链式放行；``cd`` 换到猜不出来的目录时，整条链都不放行
* **复合命令按段判定**：每一段都要单独命中，全过才整体放行
* **信任判定不是「目录名对得上」**：项目里 AI 自己能写的地方，要与已安装的
  那份逐字节相同才算
* **读自己的会话日志**：只限本项目那一个目录里的 ``*.jsonl``

用法与 hook 的脚本一样（见 ``SKILL.md``）::

    python -m pytest src/backend/tests/unit/test_sdd_hook_allow.py -q
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

#: 项目根（src/backend/tests/unit/xxx.py → 上四层）
ROOT = Path(__file__).resolve().parents[4]
HOOK_PATH = ROOT / ".claude" / "hooks" / "sdd_bash_allow.py"


def _load_hook() -> ModuleType:
    """把 hook 当模块加载进来。"""
    spec = importlib.util.spec_from_file_location("sdd_bash_allow", HOOK_PATH)
    assert spec and spec.loader, f"找不到 hook：{HOOK_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hook() -> ModuleType:
    if not HOOK_PATH.exists():
        pytest.skip(f"这个项目里没有装 hook：{HOOK_PATH}")
    return _load_hook()


# --------------------------------------------------------------------------
# 放行不了的
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # 危险可执行文件
        "rm -rf /",
        "curl http://example.com",
        "wget http://example.com",
        "sh -c 'echo hi'",
        "powershell -c echo",
        # 任意代码执行
        "python -c 'import os'",
        "python -m http.server",
        "node -e 'x'",
        # 重定向与后台
        "echo hi > /etc/passwd",
        "echo hi >> notes.md",
        "cat notes.md < /etc/passwd",
        "pytest &",
        # 命令替换
        "echo $(whoami)",
        "echo `whoami`",
        # git 的写操作与别名
        "git push",
        "git reset --hard",
        "git -c alias.status='!rm -rf /' status",
    ],
)
def test_the_hook_does_not_let_these_through(hook: ModuleType, command: str):
    assert hook.evaluate(command) is None, f"这条命令被放行了：{command}"


@pytest.mark.parametrize(
    "command",
    [
        # 只读命令的路径参数必须在项目内
        "cat ~/.ssh/id_rsa",
        "cat C:/Users/<用户名>/.ssh/id_rsa",
        "head -5 /etc/passwd",
        "grep -r secret /etc",
        # 一个布尔开关后面跟的是文件操作数，不是模式
        "sort -r /etc/passwd",
        "sort -m /etc/shadow",
        "ls -r /etc",
        # sed 不是只读命令：它的脚本里能藏 w（写文件）与 e（交给 shell 执行）
        "sed -i.bak s/a/b/ src/app/src/store.ts",
        "sed -i s/a/b/ src/app/src/store.ts",
        "sed --in-place s/a/b/ src/app/src/store.ts",
        "sed e payload.txt",
        "sed w /tmp/out.txt notes.md",
        "sed r C:/Users/<用户名>/.ssh/id_rsa notes.md",
        # cd 的形态：bash 里真的会换到家目录，hook 的 base 不能停在项目根
        "cd && cat .ssh/id_rsa",
        "cd -P ~ && cat .ssh/id_rsa",
        "cd -- ~ && cat .ssh/id_rsa",
        "cd - && cat .ssh/id_rsa",
        # 带通配符的参数也一样：.claude / .git 不读
        "cat .claude/*",
        "cat .git/*",
        "cat .claude/{settings.json}",
        "ls .claude/hooks/*",
        # 会读文件却没查路径的命令：uniq / dir 与 cat 同类
        "uniq ~/.ssh/id_rsa",
        "dir ~/.ssh",
        # 会把环境变量打进会话的命令
        "echo $ANTHROPIC_API_KEY",
        "echo ${ANTHROPIC_API_KEY}",
        # 会写文件的普通命令：白名单是自动填的，不能让它变成后门
        "cp notes.md /tmp/notes.md",
        "mv notes.md /tmp/notes.md",
        "mkdir -p somewhere",
        "tee notes.md",
    ],
)
def test_the_hook_keeps_paths_and_writes_inside_the_project(
    hook: ModuleType, command: str
):
    assert hook.evaluate(command) is None, f"这条命令被放行了：{command}"


def test_a_compound_command_is_judged_segment_by_segment(hook: ModuleType):
    """每一段都要单独命中——`pytest && rm -rf /` 不能因为第一段干净就放行。"""
    assert hook.evaluate("git status && rm -rf /") is None
    assert hook.evaluate("git status && git log --oneline -3") is not None


def test_a_denied_segment_taints_the_whole_chain(hook: ModuleType):
    """放行的那一段不能把不放行的那一段洗白。"""
    assert hook.evaluate("ls && curl http://example.com") is None


# --------------------------------------------------------------------------
# 放行得了的
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git diff",
        "ls src/backend",
        "cat specs/yuanwork/tasks.md",
        "grep -n TASK-091 specs/yuanwork/tasks.md",
        "head -20 README.md",
        "find src/backend -name '*.py'",
        "wc -l README.md",
    ],
)
def test_the_hook_lets_the_harmless_through(hook: ModuleType, command: str):
    assert hook.evaluate(command) is not None, f"这条命令被拒了：{command}"


def test_the_verification_commands_of_the_specs_run(hook: ModuleType):
    """规范里写的验证命令要真能跑——这是这个 hook 存在的理由。"""
    for command in (
        ".venv/Scripts/python.exe -m pytest src/backend/tests -q",
        "cd src/app && npm test",
        "cargo test --release --target-dir D:/cargo-target",
    ):
        assert hook.evaluate(command) is not None, f"这条命令被拒了：{command}"


def test_a_wrapper_does_not_change_what_the_command_is(hook: ModuleType):
    """`time` 与 `timeout` 只影响计时与超时，不改命令的语义。"""
    inner = ".venv/Scripts/python.exe -m pytest src/backend/tests -q"
    assert hook.evaluate("time " + inner) is not None
    assert hook.evaluate("timeout 300 " + inner) is not None
    # 剥掉包装器之后，里面那条命令仍然要自己过关
    assert hook.evaluate("time rm -rf /") is None


# --------------------------------------------------------------------------
# 信任判定
# --------------------------------------------------------------------------


def test_a_script_in_the_project_is_not_trusted_by_its_name_alone(
    hook: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """项目里 AI 自己就能写的目录，按目录信任等于开了个后门。

    这里自己造一份「安装的那份」与一份「项目里自己写的」，不比机器上
    到底装没装技能 —— 之前那条断言靠的是「文件根本不存在」，等于没测。
    """
    fake_home = tmp_path / "home"
    installed = fake_home / ".claude" / "skills" / "sdd" / "scripts"
    installed.mkdir(parents=True)
    (installed / "validate_specs.py").write_text("# 安装的那一份\n", encoding="utf-8")

    scripts = tmp_path / "proj" / "skills" / "sdd" / "scripts"
    scripts.mkdir(parents=True)
    mine = scripts / "validate_specs.py"

    monkeypatch.setattr(
        hook.os.path, "expanduser", lambda p: str(fake_home) if p == "~" else p
    )

    mine.write_text("# 我自己写的\n", encoding="utf-8")
    assert hook.script_trusted(hook.logical_path(str(mine))) is False

    # 逐字节相同 —— 技能自己的仓库满足这一点（~/.claude/skills/sdd 是联接）
    mine.write_text("# 安装的那一份\n", encoding="utf-8")
    assert hook.script_trusted(hook.logical_path(str(mine))) is True


def test_a_script_under_the_users_claude_is_trusted(
    hook: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """用户级安装的技能在 ~/.claude 下面 —— AI 写不到那里，可信。"""
    fake_home = tmp_path / "home"
    script = fake_home / ".claude" / "skills" / "sdd" / "scripts" / "validate_specs.py"
    script.parent.mkdir(parents=True)
    script.write_text("# 安装的那一份\n", encoding="utf-8")

    monkeypatch.setattr(
        hook.os.path, "expanduser", lambda p: str(fake_home) if p == "~" else p
    )
    assert hook.script_trusted(hook.logical_path(str(script))) is True


# --------------------------------------------------------------------------
# 读自己的会话日志
# --------------------------------------------------------------------------


def test_the_project_key_follows_the_harness_rule(
    hook: ModuleType, tmp_path: Path
):
    """项目键：路径里的非字母数字字符一律换成 '-'。

    留着 '.' 或 '_' 就与 harness 真实的目录名对不上 ——
    「读自己的会话日志」这个口子会静默失效（fail-closed，但功能没了）。
    """
    key = Path(hook.transcript_dir(root=str(tmp_path))).name
    assert re.fullmatch(r"[A-Za-z0-9-]+", key), f"项目键里有不该有的字符：{key}"


def test_the_agent_can_read_its_own_transcript(
    hook: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """读自己的会话日志是它自己的东西 —— 但只限本项目那一个目录。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    monkeypatch.setattr(
        hook.os.path, "expanduser", lambda p: str(fake_home) if p == "~" else p
    )
    monkeypatch.setattr(hook, "project_root", lambda: str(project))

    transcripts = Path(hook.transcript_dir())
    transcripts.mkdir(parents=True)
    mine = transcripts / "session.jsonl"
    mine.write_text("{}", encoding="utf-8")
    assert hook.is_own_transcript(str(mine)) is True

    # 别的项目的日志读不到
    other = transcripts.parent / "other-project"
    other.mkdir()
    theirs = other / "session.jsonl"
    theirs.write_text("{}", encoding="utf-8")
    assert hook.is_own_transcript(str(theirs)) is False

    # ~/.claude/settings.json 读不到
    assert hook.is_own_transcript(str(fake_home / ".claude" / "settings.json")) is False

    # 本项目目录里的非 .jsonl 读不到
    note = transcripts / "notes.md"
    note.write_text("x", encoding="utf-8")
    assert hook.is_own_transcript(str(note)) is False


def test_the_hook_never_raises(hook: ModuleType):
    """hook 崩了会把整个会话搞挂——它宁可什么都不做。"""
    for command in ("", "   ", "&&", "|||", "((((", "'unclosed"):
        hook.evaluate(command)  # 不抛异常就算过
