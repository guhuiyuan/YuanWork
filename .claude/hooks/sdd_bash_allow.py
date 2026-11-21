#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sdd_bash_allow.py — PreToolUse hook:放行 SDD 技能自己的命令

## 为什么需要它

Claude Code 的 auto 模式靠**服务端安全分类器**代替用户回答权限问题。当
``ANTHROPIC_BASE_URL`` 指向不透传该分类器调用的代理端点时,分类器不可达 ——
结果是**所有需要它判断的命令都被直接拒绝**,而不是弹窗问用户(auto 模式没有
"回退到问用户"的路径)。

官方文档(code.claude.com/docs/en/permissions):

    "When Claude Code makes a tool call, PreToolUse hooks run before the
     permission prompt… The hook output can deny the tool call, force a prompt,
     or skip the prompt to let the call proceed."

PreToolUse hook 跑在**任何权限模式检查之前**,它返回 ``permissionDecision: allow``
即可跳过权限检查 —— 整个机制纯本地,不依赖服务端分类器。hook 自己由 harness
直接执行(不经权限系统),所以分类器不可达时照样能跑。

## 装法

**推荐用安装器**:``python <技能目录>/scripts/install_hook.py --root <项目根>``
它会写好 hook 本体、白名单,并把注册条目写进项目的 ``.claude/settings.json``。

手工注册的话(命令里的路径必须是**真实存在**的绝对路径):

    {
      "hooks": {
        "PreToolUse": [
          { "matcher": "Bash",
            "hooks": [{ "type": "command",
              "command": "python \\"<绝对路径>/.claude/hooks/sdd_bash_allow.py\\"" }] }
        ]
      }
    }

也可以用 ``/hooks`` 命令交互式注册。

> ``${CLAUDE_PROJECT_DIR}`` 只在**本项目的 .claude 目录就是 Claude Code 的项目
> 目录**时才对(从子目录启动 Claude Code 时就不对),所以默认用绝对路径。

## 行为

读 stdin 的 JSON,取 ``tool_name`` 与 ``tool_input.command``:

- 命中白名单 → stdout 输出 ``permissionDecision: allow``,exit 0
- 未命中     → **不输出任何内容**,exit 0(交回正常权限流程)

输出**必须是纯 ASCII**(json.dumps(..., ensure_ascii=True)):Windows 上 Python 的
stdout 默认是 GBK,中文理由会变成 GBK 字节,harness 按 UTF-8 读就是乱码 ——
实测出现过「Hook output does not start with {, treating as plain text」。

**任何异常都不向外抛** —— hook 崩了会把整个会话搞挂,宁可什么都不做。

## 写文件工具(Write / Edit / MultiEdit / NotebookEdit)

auto 模式下写文件本来有内建短路(「若在 acceptEdits 模式下会被放行」),
本 hook 的路径判定是**兜底**:分类器不可达时也能写项目内的文件。判定只看**路径**:

- 目标在**项目目录内** → 放行
- 目标在项目目录外 → 不放行
- 路径里**任何一段**叫 **``.claude``** 或 **``.git``** → **不放行**
  (``.claude/settings.json`` 能给自己授权权限,``.git/config`` 能改 hook —— 这两处
  必须留给人手动改,不能由 AI 自动放行)
- 白名单文件里写了 ``"write_paths"`` → 只放行这些子路径(相对项目根或绝对路径);
  写成一堆空串时**一律不放行**(fail-closed)

项目根怎么来:``CLAUDE_PROJECT_DIR`` 环境变量优先;没有就从 hook 自身路径往上找
**含 ``.claude/`` 的那一层**(装在 ``<项目>/.claude/hooks/`` 下时正好是项目根)。
两个都拿不到 → **不放行**(失败时收紧,不是放宽)。

## 派发子 agent(Agent / Task)

auto 模式下「派发子 agent」这一步本身也要服务端分类器 → 分类器不可达时
**连派发都被拒**(实测「Agent tool permission denied」),SDD 的并行实现/校验
就没法用。所以 hook 也放行这一步。

放行的只是**派发动作**:子 agent 继承会话级设置,**内部**跑命令仍走这份白名单
(Bash 只放白名单命令、写文件只放项目内),其他工具仍要服务端分类器。

## 白名单(保守)

1. SDD 技能自己的脚本:python 解释器 + 路径**整段**命中 ``skills/sdd/scripts/``
   或 ``.claude/sdd/`` 的 ``.py`` 文件,**且**落在可信根(项目根 / 工作目录 /
   ``~/.claude``)下 —— 子串比对会让 ``/tmp/evil/skills/sdd/scripts/x.py`` 蒙混过关;
   而 ``skills/sdd/scripts/`` **只认固定文件名**(见 SDD_SCRIPT_NAMES)——
   那个目录 AI 自己就能写,按目录信任等于给它一个「写个 .py 再执行」的口子
2. git 的安全子命令:status/diff/log/show/rev-parse/branch/add/commit
3. 只读命令(ls/cat/head/grep/find/diff/…):单个跑本来就会被内建无害命令表
   放行,这里补的是**链式形态**里的它们
4. 额外命令:读 ``sdd-hook-allow.json`` 的 ``extra`` 列表(前缀匹配)

**复合命令按段判定**

一条命令里有 ``&&`` ``||`` ``;`` ``|`` 或换行时,按 shell 习惯在**引号外**拆成
若干段,**每段都要单独通过判定,全过才整体放行**:

- ``cd X && pytest`` / ``sleep 5 && cd X && pytest`` / ``cat f | head -20``
  → 放行(``cd`` 段直接跳过;只读段按路径判定)
- ``python x.py && rm -rf /`` → **不放行** —— ``rm`` 那一段过不了,
  整条就不放行

**拆段必须与 bash 一致** —— 反斜杠转义是实测出来的洞:bash 里 ``\\"`` 是
「一个字面引号」,**不**开启引号状态;只认引号的拆段器会把它当成开引号,
于是后面的 ``&& rm -rf /`` 全被吞进第一段,第二段压根没进判定
(实测 ``true \\" && git push`` 被整条放行,``git push`` 真的执行了)。
现在引号、反斜杠、``2>&1`` 与 ``>&file`` 的处理都对齐 bash(见 scan())。

**只读命令的路径判定**(fail-closed,任何「不确定」都按不放行处理):

- 有效工作目录(链里 ``cd`` 之后的目录)必须在项目内
- 路径参数要展开环境变量(``$HOME``)、``~`` 与 Git-Bash 风格绝对路径
  (``/c/Users/...``);**展开不出确定值就不放行**
- 含通配符的参数:展开结果不可知 → 只放行「相对路径 + 目录部分在项目内」
- 选项里的路径(``--from-file=…`` ``--output=…``)同样要过这一关
- 目标路径里不能有 ``.claude`` / ``.git`` 段

**不放行**:

- rm / curl / wget / sudo / chmod 等危险可执行文件 —— **即使写进白名单也不放行**
- **shell 与脚本解释器**(sh / bash / cmd / powershell / perl / ruby / node / npx /
  osascript / wscript …)与**包装器**(env / xargs):它们能执行任意命令,
  白名单里写一条 ``sh -c "任意命令"`` 就是开后门
- ``find`` 带 ``-exec`` / ``-delete`` / ``-fprintf`` / ``-fls``
- git push / reset / clean,以及任何会改变 git 行为的**前置选项**
  (``-c`` 能定义 alias、``--exec-path`` 能换掉子命令查找路径);
  ``-C`` 指向**别的仓库**且子命令会写(add/commit/branch)时也不放行;
  ``--output`` / ``--no-index`` 也不放行(前者能写任意路径,后者能读任意文件)
- 任意 python(``python -c`` / ``python -m 任意模块``);解释器必须钉死脚本
- 太松的白名单模式(例如白名单里写一条 ``python`` 或一条 ``sh``)
- **文件重定向**(``>`` ``>>`` ``<``)—— 能写文件;
  ``>&file`` 是「重定向到文件」而不是 fd 复制,同样不放行
- **后台运行**(单个 ``&``)—— 脱离监督
- **命令替换**(``$( )`` 与反引号)—— 在**整条命令**上查,包括 ``cd`` 的参数里:
  ``cd $(whoami) && pytest`` 曾经在剥掉 cd 前缀后逃过检查
- **引号没闭合**的整条命令(shell 会报语法错)

## 安全边界

- 本 hook 的 allow **不能覆盖** ``permissions.deny`` / ``ask`` 规则 ——
  Claude Code 里 deny/ask 优先于 hook 的返回值,所以不会削弱已有安全规则
- 删掉本文件与 settings.json 里的条目即可完全回退
- 设 ``SDD_HOOK_LOG=1`` 可把放行记录写到同目录 ``sdd-hook-audit.log``
- **hook 不是沙箱**:能写项目内文件的 AI,仍然可以往「白名单会执行的脚本」里
  塞代码(比如 pytest 的 conftest)。这一层由 ``permissions.deny`` 与人工审阅兜底
"""

import json
import os
import sys

ALLOWLIST_FILE = 'sdd-hook-allow.json'
AUDIT_FILE = 'sdd-hook-audit.log'

# SDD 脚本所在目录 —— 按**目录段**匹配,不是子串匹配。
#
# 「子串匹配」是实测出来的洞:`python /tmp/evil/skills/sdd/scripts/payload.py`
# 因为路径里含有 "skills/sdd/scripts/" 就被当成 SDD 脚本放行了。
# 现在要求这几个词各自独立成段,并且脚本必须落在**可信根**下
# (项目根 / 当前工作目录 / 用户的 ~/.claude),见 trusted_roots()。
SCRIPT_DIR_SEGMENTS = (
    ('skills', 'sdd', 'scripts'),
    ('.claude', 'sdd'),
)

# SDD 技能自己的脚本 —— **按文件名**信任,不是按目录信任。
#
# 按目录信任是循环论证:``skills/sdd/scripts/`` 就在项目目录里,而写文件工具
# 允许写项目内的任意路径 → AI 自己写一个 .py 就能被当成「自家脚本」执行,
# 等于开了一个任意代码执行的口子(实测:Write 一个 payload.py → python 它 →
# 真的执行了)。所以这个目录只认下面这几个固定文件名。
#
# ``.claude/sdd/`` 那条不受影响:它在 ``.claude/`` 下面,写文件工具本来就
# 拒写整个 ``.claude/``,AI 改不了它。
SDD_SCRIPT_NAMES = frozenset({
    'init_specs.py',
    'validate_specs.py',
    'update_claude_md.py',
    'verify_acceptance.py',
    'export_openspec.py',
    'install_hook.py',
    'hook_probe.py',
})

# git 的安全子命令 —— 刻意不含 push / remote / reset / clean
GIT_SAFE_SUBCOMMANDS = frozenset({
    'status', 'diff', 'log', 'show', 'rev-parse', 'branch', 'add', 'commit',
})

# 会**写**仓库的 git 子命令 —— 配合 -C 时必须限制在项目内
GIT_WRITING_SUBCOMMANDS = frozenset({'add', 'commit', 'branch'})

# 会写文件或读任意文件的 git 选项 —— 一律不放行
#   --output=FILE      把 diff 写进任意路径(实测能写 .claude/ 与 .git/)
#   --no-index         让 git diff 读任意两个文件
#   --pathspec-from-file  从任意文件读路径列表
GIT_WRITE_OPTIONS = frozenset({
    '--output', '--no-index', '--output-directory', '--pathspec-from-file',
})

# 内置的默认额外白名单:各语言"跑本项目测试"的标准命令。
# 阶段 4 的每个 Task 都要跑验证命令,这些是最常见的形态;项目自己的
# sdd-hook-allow.json 可以再加。
DEFAULT_EXTRA = (
    'pytest',
    'python -m pytest',
    'python -m unittest',
    'npm test',
    'npm run test',
    'cargo test',
    'go test',
)

# 危险可执行文件 —— 即使用户把它写进白名单也不放行。
# hook 对外承诺「不放行 rm / curl / git push / 任意 python」,这句承诺必须成立:
# 白名单是用户可编辑的,不能让它变成绕过承诺的后门。
#
# 名单里必须包含**能执行任意命令的壳与解释器**:只查 argv[0] 时,
# 白名单里写一条 `sh -c "rm -rf /"` 就能整条放行(实测)。
#
# 说明:白名单是**前缀**匹配,所以「timeout 300 pytest」这类前缀仍然钉死;
# 真正危险的是"短前缀 + 能吃命令字符串"的那些 —— 它们在下面。
DENIED_EXECUTABLES = frozenset({
    # 删文件 / 改权限
    'rm', 'rmdir', 'del', 'erase', 'mkfs', 'dd', 'shred', 'format', 'diskpart',
    'sudo', 'su', 'doas', 'chmod', 'chown', 'chgrp',
    # 网络 / 远程
    'curl', 'wget', 'nc', 'ncat', 'socat', 'telnet', 'ssh', 'scp', 'sftp',
    # shell(能跑任意命令)
    'sh', 'bash', 'zsh', 'dash', 'ksh', 'csh', 'tcsh', 'fish', 'busybox', 'cmd',
    'powershell', 'pwsh', 'eval', 'exec', 'source', 'command',
    # 脚本解释器
    'perl', 'ruby', 'php', 'node', 'npx', 'deno', 'bun', 'osascript',
    'wscript', 'cscript', 'mshta', 'rundll32', 'regsvr32', 'wine',
    # 包装器
    'env', 'xargs',
})

# find 本身常用(找文件),但 -exec / -delete 能执行命令,
# -fprintf / -fls 能把内容写进任意文件 → 见到这些参数就拒
FIND_EXEC_FLAGS = ('-exec', '-execdir', '-ok', '-okdir', '-delete',
                   '-fprintf', '-fls')

# git 唯一允许出现在子命令**之前**的选项 —— 只改工作目录,无害。
#
# 其余一律拒绝,原因很实在:
#   -c / --config-env   能定义 alias,把"安全子命令"换成任意 shell
#                      (git -c alias.status='!rm -rf /' status)
#   --exec-path        能换掉 git 查找子命令的目录
#   --git-dir/--work-tree 能把操作指向另一个仓库
GIT_ALLOWED_VALUE_OPTIONS = frozenset({'-C'})

# 视为 python 解释器的可执行文件名
# 注意不能写成 startswith('py') —— 那会把 pytest / pylint 也当成解释器
PYTHON_EXACT = ('py',)
PYTHON_PREFIX = ('python',)

# 这些 python 选项后面还跟一个值,跳选项时要把值一起跳过
PYTHON_VALUE_OPTIONS = frozenset({'-X', '-W', '--check-hash-based-pycs'})

# 只读命令 —— 与 Claude Code 内建的「无害命令表」对齐。
#
# 单个跑这些命令本来就会被 harness 放行(不需要分类器);这里补的是**复合命令链**
# 里的它们:链形态不再适用内建的只读判定,于是要由 hook 逐段放行。
#
# 刻意**不含** env / printenv / set —— 那些会把环境变量(含 API key)印出来。
READONLY_COMMANDS = frozenset({
    'ls', 'dir', 'pwd', 'cat', 'head', 'tail', 'wc', 'grep', 'egrep', 'fgrep',
    'diff', 'cmp', 'sort', 'uniq', 'cut', 'tr', 'basename', 'dirname', 'realpath',
    'stat', 'file', 'du', 'df', 'tree', 'which', 'type', 'whoami', 'date',
    'sleep', 'test', 'true', 'false', 'echo', 'find', 'md5sum', 'sha256sum',
})

# 会**读文件**的只读命令 —— 它们的路径参数必须落在项目内,
# 否则 `cat ~/.ssh/id_rsa` 这种读密钥的路径会被链式放行。
# ls 也在里面:`ls -l $HOME/.ssh` 会泄露密钥目录的结构。
READONLY_FILE_COMMANDS = frozenset({
    'cat', 'head', 'tail', 'wc', 'grep', 'egrep', 'fgrep', 'diff', 'cmp',
    'sort', 'stat', 'file', 'find', 'du', 'tree', 'cut', 'md5sum', 'sha256sum',
    'ls',
})

# 带文件路径的工具 —— 放行与否只看路径,不看命令
PATH_TOOLS = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit')

# 派发子 agent 的工具 —— 放行的是**派发动作**这一步。
#
# 子 agent 继承会话级设置,所以它**内部**跑的命令仍然走同一份白名单
# (Bash 只放白名单命令、写文件只放项目内),其他工具仍要服务端分类器。
# 没有这一步,auto 模式下分类器不可达时连"派发"都会被拒(实测
# 「Agent tool permission denied」),SDD 的并行实现/校验就没法用。
AGENT_TOOLS = ('Agent', 'Task')

# 项目目录内也不自动放行的子目录:
#   .claude/ —— settings.json 能给自己授权权限
#   .git/    —— config 能改 hook、index 能改工作区状态
# 这两处必须留给人手动改
WRITE_DENY_DIRS = ('.claude', '.git')


def _is_python_exe(token):
    base = os.path.basename(token).lower()
    if base.endswith('.exe'):
        base = base[:-4]
    if base in PYTHON_EXACT:
        return True
    return any(base.startswith(prefix) for prefix in PYTHON_PREFIX)


def scan(cmd):
    """按 **shell 习惯**扫描一条命令。

    返回 ``(segments, writes, unterminated)``:

    - ``segments`` —— 每段是一个 token 列表;切分点是引号外的
      ``&&`` ``||`` ``;`` ``|`` 与换行
    - ``writes`` —— 命令里有文件重定向或后台运行
    - ``unterminated`` —— 引号没闭合(shell 会报语法错,一律按不放行处理)

    **反斜杠转义必须按 bash 处理** —— 这是实测出来的洞:
    bash 里 ``\\"`` 是「一个字面引号」,**不**开启引号状态;而只认引号的拆段器
    会把它当成开引号,于是后面的 ``&& rm -rf /`` 全被吞进第一段,第二段
    压根没进判定 → ``true \\" && git push`` 被整条放行(实测第二段真的执行了)。

    规则(与 bash 一致):

    - 引号外:``\\`` 转义下一个字符(该字符原样入词)
    - 单引号内:全部字面,没有转义
    - 双引号内:``\\`` 只对 ``$ `` `` ` `` ``"`` ``\\`` 与换行有转义意义
    """
    segments = []
    raws = []
    tokens = []
    cur = ''
    quote = None
    writes = False
    i, n = 0, len(cmd)
    seg_start = 0

    def end_token():
        nonlocal cur
        if cur:
            tokens.append(cur)
            cur = ''

    def end_segment(upto):
        end_token()
        if tokens:
            segments.append(list(tokens))
            raws.append(cmd[seg_start:upto].strip())
            del tokens[:]

    while i < n:
        ch = cmd[i]
        if quote == "'":
            # 单引号内全是字面量,没有转义
            if ch == "'":
                quote = None
            else:
                cur += ch
            i += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = None
                i += 1
                continue
            if ch == '\\':
                nxt = cmd[i + 1:i + 2]
                if nxt in ('$', '`', '"', '\\'):
                    cur += nxt
                    i += 2
                    continue
                if nxt == '\n':
                    i += 2
                    continue
                cur += ch      # 双引号内 \ 跟别的字符 → 反斜杠是字面量
                i += 1
                continue
            cur += ch
            i += 1
            continue
        # ---- 引号外 ----
        if ch == '\\':
            nxt = cmd[i + 1:i + 2]
            if nxt == '\n':
                i += 2
                continue
            if nxt:
                cur += nxt      # 转义下一个字符,原样入词
                i += 2
                continue
            cur += ch
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if cmd[i:i + 2] in ('&&', '||'):
            end_segment(i)
            i += 2
            seg_start = i
            continue
        if ch in (';', '|', '\n', '\r'):
            end_segment(i)
            i += 1
            seg_start = i
            continue
        if ch == '>':
            # fd 复制只认 `>&` 后面跟数字或 `-`(2>&1 / 2>&-)。
            # `>&file` 是把输出**重定向到那个文件**(会创建并截断它)——
            # 实测 `echo x 1>&~/.bashrc` 就是任意路径写原语,不能放过。
            if cmd[i + 1:i + 2] == '&':
                after = cmd[i + 2:i + 3]
                if after.isdigit() or after == '-':
                    cur += '>&'
                    i += 2
                    continue
            writes = True
            cur += ch
            i += 1
            continue
        if ch == '<':
            writes = True
            cur += ch
            i += 1
            continue
        if ch == '&':
            writes = True          # 后台运行
            cur += ch
            i += 1
            continue
        if ch.isspace():
            end_token()
            i += 1
            continue
        cur += ch
        i += 1

    end_segment(i)
    return segments, raws, writes, quote is not None


def tokenize(cmd):
    """切词(按 shell 规则处理引号与转义,不做变量展开)。"""
    segments, _, _, _ = scan(cmd)
    return segments[0] if segments else []


def canon_path(path):
    """把路径规范化成可比对的字符串。

    解析软链接/目录联接(realpath),统一大小写与分隔符 —— 这样
    ``<项目>/a/../../etc`` 与 ``<项目>/link -> C:\\Windows`` 都会露馅。
    """
    if not isinstance(path, str) or not path.strip():
        return ''
    try:
        real = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except Exception:
        return ''
    text = os.path.normcase(real).replace('\\', '/')
    if len(text) > 1 and text.endswith('/'):
        text = text[:-1]
    return text


def within(root, target):
    """target 是否在 root 之内(root 自身也算)。"""
    if not root or not target:
        return False
    if target == root:
        return True
    return target.startswith(root + '/')


def contains_segments(path, segments):
    """路径里是否含有一段**连续的目录段**(整段比对,不是子串)。

    ``skills/sdd/scripts`` 只在真的是这三个目录时才命中 ——
    子串比对会让 ``/tmp/evil/skills/sdd/scripts/x.py`` 也算命中。
    """
    parts = [p for p in path.replace('\\', '/').split('/') if p]
    want = tuple(segments)
    for i in range(len(parts) - len(want) + 1):
        if tuple(parts[i:i + len(want)]) == want:
            return True
    return False


def trusted_roots():
    """可信根:「SDD 自己的脚本」必须落在这些目录下面。

    - 项目根(hook 自己所在位置往上找含 .claude/ 的那一层)
    - 当前工作目录(从子目录启动时的兜底)
    - 用户的 ``~/.claude``(用户级 / plugin 安装的技能都在这下面)

    没有可信根这一层,路径里带 ``skills/sdd/scripts/`` 的**任意**文件
    都会被当成自家脚本放行(实测:/tmp/evil/skills/sdd/scripts/x.py)。
    """
    roots = []
    for path in (project_root(), os.getcwd(),
                 os.path.join(os.path.expanduser('~'), '.claude')):
        if not path:
            continue
        cand = canon_path(path)
        if cand and cand not in roots:
            roots.append(cand)
    return roots


def has_substitution(cmd):
    """命令里有没有命令替换 —— ``$(`` 与反引号,在**任何位置**都算不安全。

    必须在剥掉 ``cd <路径> &&`` 之前查:命令替换藏在 cd 的参数里时
    (例如 ``cd $(whoami) && pytest``),剥掉前缀之后就看不见了 —— 实测会被放行。
    双引号里照样会被 shell 展开,所以引号内外一律算不安全。
    """
    return '`' in cmd or '$(' in cmd


def split_segments(cmd):
    """把一条命令拆成若干独立命令段。

    切分与转义规则全交给 scan()(与 bash 一致),这里只做包装:

    返回 ``(段列表, 段原文列表, 是否有写操作, 引号是否未闭合)``。

    - 文件重定向(``>`` ``>>`` ``<``)→ 写操作,整条不放行
    - 后台运行(单个 ``&``)→ 脱离监督,整条不放行
    - ``2>&1`` 这种 **fd 复制**不算写操作(它只是把 stderr 并到 stdout)
    - 引号未闭合 → shell 会报语法错,一律不放行
    """
    segments, raws, writes, unterminated = scan(cmd)
    return segments, raws, writes, unterminated


# 通配符与花括号 —— 展开结果不可知,不能当普通路径处理
GLOB_CHARS = ('*', '?', '[', ']', '{', '}')


def has_glob(text):
    return any(ch in text for ch in GLOB_CHARS)


def looks_like_path(token):
    """这个参数像不像一个路径。"""
    if not token:
        return False
    if token.startswith('~'):
        return True
    if '/' in token or '\\' in token:
        return True
    return os.path.isabs(token)


# 看起来像 HTTP 方法的词 —— 规范里「外加一次核对:`GET /api/x`」这种是接口说明
COMMAND_STOPWORDS = frozenset({
    'GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS',
})


def looks_like_command(text):
    """这段文本像不像一条能跑的命令。

    规范里的「验证命令」字段常常写成散文:
    `` `cmd` 与 `cmd2`，外加一次核对(`GET /api/x`) ``
    —— 后面那些反引号片段是**说明**,不是命令,不能进白名单。

    判据:

    - 含中文 → 散文
    - 第一个词是 HTTP 方法 → 接口说明
    - 没有空格 → 不是一条完整命令(单个词由内置白名单覆盖)
    """
    if not text:
        return False
    if any('一' <= ch <= '鿿' for ch in text):
        return False
    parts = text.split()
    if len(parts) < 2:
        return False
    if parts[0].upper() in COMMAND_STOPWORDS:
        return False
    return True


def trusted_dirs():
    """可以当作「项目内」的可信目录:项目根 + 当前工作目录。"""
    out = []
    for path in (project_root(), os.getcwd()):
        canon = canon_path(path)
        if canon and canon not in out:
            out.append(canon)
    return out


def has_denied_segment(path):
    """路径里有没有 .claude / .git 这样的目录段。"""
    return any(part in WRITE_DENY_DIRS for part in path.split('/'))


def expand_path_token(tok):
    """把路径参数规范化:展开环境变量、``~`` 与 MSYS 风格路径。

    展开不出确定值(未定义变量、``~user``)时返回 ``''`` —— 调用方按
    「无法验证 → 不放行」处理(fail-closed)。

    实测出来的洞:``cat $HOME/.ssh/id_ed25519`` 若不展开,会被当成项目内的
    相对路径放行,实际打印的是真私钥。
    """
    if not tok:
        return ''
    text = tok
    if '$' in text or '%' in text:
        text = os.path.expandvars(text)
        if '$' in text or '%' in text:
            return ''          # 未定义的变量 → 无法验证
    if text.startswith('~'):
        text = os.path.expanduser(text)
        if text.startswith('~'):
            return ''          # ~user 形式在 Windows 上不展开
    # Git Bash / MSYS 风格绝对路径:/c/Users/... → C:/Users/...
    if len(text) >= 3 and text[0] == '/' and text[1].isalpha() and text[2] == '/':
        text = '%s:%s' % (text[1].upper(), text[2:])
    return text


def resolve_against(token, base):
    """把参数解析成绝对路径(相对 base 目录)。

    ``~`` 与绝对路径按原样解析;相对路径挂在 base 下面。
    """
    text = expand_path_token(token)
    if not text:
        return ''
    if os.path.isabs(text):
        return canon_path(text)
    try:
        return canon_path(os.path.join(base, text))
    except Exception:
        return ''


def resolve_dir(target, base):
    """解析 cd 的目标目录(相对当前 base)。"""
    if not target or target == '-':
        return base
    resolved = resolve_against(target, base)
    return resolved or base


def path_args_inside(tokens, base=None):
    """只读命令的路径参数必须落在**项目目录**内。

    规则(fail-closed):

    1. 有效工作目录(base)必须在项目内 —— 已经 cd 到项目外就不放行
       (否则 ``cd ~/.ssh && cat id_rsa`` 会漏掉)
    2. 每个路径样式的参数都要解析到项目内,且路径里不能有 .claude / .git
    3. 含通配符的参数:展开结果不可知 → 只放行「相对路径 + 目录部分在项目内」
    4. 选项里的路径(``--from-file=…`` ``--output=…``)同样要过这一关
    """
    base_dir = base or os.getcwd()
    roots = trusted_dirs()
    if not roots:
        return False
    base_canon = canon_path(base_dir)
    if not base_canon or not any(within(r, base_canon) for r in roots):
        return False          # 已经离开项目 → 整段不放行
    for tok in tokens[1:]:
        value = tok
        if tok.startswith('-'):
            if '=' not in tok:
                continue       # 单独的选项,值在下一个 token
            value = tok.split('=', 1)[1]
            if not value:
                continue
        if not value:
            continue
        if not (looks_like_path(value) or has_glob(value)):
            # 不是路径样子的参数:只有在当前目录下**确实存在**时才按路径算
            # (否则 grep 的模式词会被误判成路径)
            try:
                exists = os.path.exists(os.path.join(base_dir, value))
            except Exception:
                exists = False
            if not exists:
                continue
        if has_glob(value):
            # 通配符:展开结果不可知。只放行「相对路径 + 目录部分在项目内」
            if os.path.isabs(value) or value.startswith('~'):
                return False
            cut = len(value)
            for ch in GLOB_CHARS:
                pos = value.find(ch)
                if pos >= 0:
                    cut = min(cut, pos)
            head = value[:cut]
            dirpart = head[:head.rfind('/') + 1] if '/' in head else ''
            if dirpart:
                target = resolve_against(dirpart.rstrip('/'), base_dir)
                if not target or not any(within(r, target) for r in roots):
                    return False
            continue
        target = resolve_against(value, base_dir)
        if not target:
            return False      # 展开不出确定值 → 不放行
        if not any(within(r, target) for r in roots):
            return False
        if has_denied_segment(target):
            return False      # .claude / .git
    return True


def match_sdd_script(tokens):
    """命令行是否是"python 解释器 + SDD 自己的脚本"。命中返回脚本路径。

    两道关:
    1. 目录名要**整段**命中 skills/sdd/scripts(或 .claude/sdd)—— 子串比对
       会让 /tmp/evil/skills/sdd/scripts/x.py 也算命中(实测)。
    2. 脚本必须落在**可信根**下(项目根 / 工作目录 / ~/.claude)——
       否则任何路径里带这个目录名的文件都会被当成自家脚本。
    """
    if len(tokens) < 2 or not _is_python_exe(tokens[0]):
        return None
    rest = tokens[1:]
    idx = 0
    while idx < len(rest) and rest[idx].startswith('-'):
        if rest[idx] in ('-c', '-m', '-'):
            return None  # 内联代码 / 模块执行 → 任意代码,不放行
        idx += 2 if rest[idx] in PYTHON_VALUE_OPTIONS else 1
    if idx >= len(rest):
        return None
    cand = rest[idx]
    # 归一化后再比对,免得 `skills/sdd/scripts/../../evil.py` 这类路径穿越蒙混过关
    norm = canon_path(cand)
    if not norm or not norm.endswith('.py'):
        return None
    if not any(contains_segments(norm, segs) for segs in SCRIPT_DIR_SEGMENTS):
        return None
    if not any(within(root, norm) for root in trusted_roots()):
        return None
    # skills/sdd/scripts/ 只认固定的文件名 —— 那个目录 AI 自己就能写,
    # 按目录信任等于给它一个「写一个 .py 再执行它」的口子(实测过)。
    if contains_segments(norm, ('skills', 'sdd', 'scripts')):
        if os.path.basename(norm).lower() not in SDD_SCRIPT_NAMES:
            return None
    # 展示用原始大小写(canon_path 会把 Windows 路径转小写,看着不像真路径)
    return os.path.normpath(cand).replace('\\', '/')


def match_git(tokens, base=None):
    """命令行是否是 git 的安全子命令。命中返回 "git <子命令>"。

    两处收紧(都是实测出来的洞):

    - ``-C <别的仓库>`` + 会写的子命令(git add / commit / branch)→ 不放行。
      ``-C`` 的作用范围和 ``--git-dir`` 等价:命令对哪个仓库生效完全由它决定,
      放着不管等于借白名单往别的项目的 .git/ 里写。只读子命令(status/diff/log…)
      无所谓,目标在外面也放行。
    - 子命令后面带 ``--output`` / ``--no-index`` 等 → 不放行。
      ``git diff --output=<路径>`` 能把内容写进任意路径(含 .claude/ 与 .git/),
      ``git diff --no-index a b`` 能读任意两个文件。
    """
    if not tokens:
        return None
    name = os.path.basename(tokens[0]).lower()
    if name.endswith('.exe'):
        name = name[:-4]
    if name != 'git':
        return None
    idx = 1
    sub = None
    c_target = ''
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in GIT_ALLOWED_VALUE_OPTIONS:
            if idx + 1 >= len(tokens):
                return None
            c_target = tokens[idx + 1]
            idx += 2
            continue
        if tok.startswith('-'):
            # 任何别的选项都可能改变 git 的行为,一律不放行
            return None
        sub = tok
        idx += 1
        break
    if sub not in GIT_SAFE_SUBCOMMANDS:
        return None
    for tok in tokens[idx:]:
        if tok.startswith('-') and tok.split('=', 1)[0] in GIT_WRITE_OPTIONS:
            return None
    if c_target and sub in GIT_WRITING_SUBCOMMANDS:
        resolved = resolve_against(c_target, base or os.getcwd())
        roots = trusted_dirs()
        if not resolved or not any(within(r, resolved) for r in roots):
            return None
    return 'git %s' % sub


def allowlist_candidates():
    """白名单文件的所有可能位置。

    顺序即优先级:
    1. ``SDD_HOOK_ALLOWLIST`` 环境变量指定的路径
    2. 项目目录 ``$CLAUDE_PROJECT_DIR/.claude/hooks/``(hook 被引用到别处时,
       项目自己那份才是对的)
    3. 脚本同目录(hook 装在项目里时两者是同一个文件)
    """
    out = []
    explicit = os.environ.get('SDD_HOOK_ALLOWLIST')
    if explicit:
        out.append(explicit)
    project = os.environ.get('CLAUDE_PROJECT_DIR')
    if project:
        out.append(os.path.join(project, '.claude', 'hooks', ALLOWLIST_FILE))
    out.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ALLOWLIST_FILE))
    seen, uniq = set(), []
    for path in out:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            uniq.append(path)
    return uniq


def load_allowlist():
    """读白名单。读不到就当只有内置默认项。

    返回 (extra, tools, write_paths):
    - extra       —— 额外放行的命令前缀
    - tools       —— 额外放行的工具名(整类放行,不看参数)
    - write_paths —— 写文件时允许的子路径;为空 = 整个项目目录
    """
    extra, tools, write_paths = list(DEFAULT_EXTRA), [], []
    for path in allowlist_candidates():
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get('extra') or []:
            text = str(item)
            if text not in extra:
                extra.append(text)
        for item in data.get('tools') or []:
            text = str(item)
            if text not in tools:
                tools.append(text)
        for item in data.get('write_paths') or []:
            text = str(item)
            if text not in write_paths:
                write_paths.append(text)
    return extra, tools, write_paths


def pattern_is_loose(pattern):
    """模式太松,会把任意代码执行也放进来。

    例如白名单里写一条 ``python`` —— 它能前缀匹配 ``python -c "任意代码"``,
    等于开了一个后门。解释器要么钉死脚本、要么钉死模块名,才算合格的模式。

    危险可执行文件开头的模式同样算太松:``sh`` 会前缀匹配 ``sh -c "任意命令"``。
    """
    parts = pattern.split()
    if not parts:
        return True
    exe = os.path.basename(parts[0]).lower()
    if exe.endswith('.exe'):
        exe = exe[:-4]
    if exe in DENIED_EXECUTABLES:
        return True              # 'sh' / 'bash' / 'env' / 'rm' … 一律跳过
    if exe in PYTHON_EXACT or any(exe.startswith(p) for p in PYTHON_PREFIX):
        if len(parts) == 1:
            return True          # 'python'
        if parts[1] == '-c':
            return True          # 'python -c'
        if parts[1] in ('-m', '-') and len(parts) < 3:
            return True          # 'python -m' / 'python -'
    return False


def match_extra(command, tokens, extra):
    """额外命令前缀匹配。"""
    if not extra:
        return None
    normalized = ' '.join(tokens)
    for pattern in extra:
        pattern = pattern.strip()
        if not pattern or pattern_is_loose(pattern):
            continue
        for candidate in (command.strip(), normalized):
            if not candidate:
                continue
            if candidate == pattern:
                return pattern
            if candidate.startswith(pattern + ' '):
                return pattern
            # 以分隔符结尾的模式(如 "pytest tests/")按前缀匹配
            if pattern[-1] in '/\\-_' and candidate.startswith(pattern):
                return pattern
    return None


# ---------- 写文件工具(Write / Edit / MultiEdit / NotebookEdit) ----------

def project_root():
    """推断项目根目录;推断不出来返回 ''。

    1. ``CLAUDE_PROJECT_DIR`` 环境变量(harness 给的)
    2. 从 hook 自身路径往上找 —— 第一个「含 ``.claude`` 子目录」的层
       (装在 ``<项目>/.claude/hooks/`` 下时正好就是项目根)
    """
    env = os.environ.get('CLAUDE_PROJECT_DIR')
    if env:
        cand = canon_path(env)
        if cand:
            return cand
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        parent = os.path.dirname(here)
        if not parent or parent == here:
            break
        if os.path.isdir(os.path.join(parent, '.claude')):
            return canon_path(parent)
        here = parent
    return ''


def write_allowed(file_path, root, write_paths=None):
    """写入目标是否允许。

    ``write_paths`` 给定时只放行这些子路径;**给了一堆空串时一律拒绝**
    (fail-closed:配置写坏了不能变成"整个项目目录都能写")。
    """
    if not file_path or not root:
        return False
    root = canon_path(root)
    target = canon_path(file_path)
    if not root or not target:
        return False
    if write_paths:
        roots = []
        for extra in write_paths:
            text = str(extra).strip()
            if not text:
                continue
            cand = canon_path(text if os.path.isabs(text) else os.path.join(root, text))
            if cand:
                roots.append(cand)
        if not roots:
            return False  # 写了但全是空串 → 收紧失败,不放行
    else:
        roots = [root]  # 没指定子路径 = 整个项目目录
    if not any(within(r, target) for r in roots):
        return False
    # 路径里**任何一段**叫 .claude / .git 都不放行 —— 不只项目根底下那一个,
    # 嵌套仓库 / 联接目录里的 .claude 同样要挡住(见 WRITE_DENY_DIRS)
    for part in target.split('/'):
        if part in WRITE_DENY_DIRS:
            return False
    return True


def evaluate_tool(tool_name, tool_input, tools=None, root=None, write_paths=None):
    """判定非 Bash 工具。命中返回理由,否则返回 None。

    写文件类工具**只看路径**:在项目目录内就放行,目录外或落在
    ``.claude/`` / ``.git/`` 里就不放行。
    """
    if not tool_name:
        return None
    if tool_name in AGENT_TOOLS:
        # 只放行"派发"这一步;子 agent 内部跑什么仍由白名单约束
        return '派发子 agent: %s' % tool_name
    if tool_name in PATH_TOOLS:
        # 写文件类工具**只能**走路径判定 —— 白名单的 tools 列表不得整类放行,
        # 否则白名单里写一条 "Write" 就能绕过 .claude / .git 的拒写名单
        if not isinstance(tool_input, dict):
            tool_input = {}
        path = tool_input.get('file_path') or tool_input.get('notebook_path') or ''
        if root is None:
            root = project_root()
        if not root:
            return None
        if write_allowed(path, root, write_paths):
            return '项目目录内的文件写入: %s' % path
        return None
    if tools and tool_name in tools:
        return '额外白名单工具: %s' % tool_name
    return None


def evaluate_segment(tokens, extra, base=None, raw=None):
    """判定**单个**命令段。命中返回理由,否则返回 None。

    ``tokens`` 是按 shell 规则切出来的词;``raw`` 是这一段的原文
    (白名单里手写的、带引号的模式要靠它才匹配得上)。
    ``base`` 是链里前面 ``cd`` 之后的当前目录,只读命令的路径判定要用。
    """
    if not tokens:
        return None
    # 危险可执行文件一律不放行 —— 白名单是用户可编辑的,不能成为后门
    exe = os.path.basename(tokens[0]).lower()
    if exe.endswith('.exe'):
        exe = exe[:-4]
    if exe in DENIED_EXECUTABLES:
        return None
    # find 本身常用(找文件),但 -exec / -delete / -fprintf / -fls 能执行命令或写文件
    if exe == 'find' and any(tok in FIND_EXEC_FLAGS for tok in tokens[1:]):
        return None
    hit = match_sdd_script(tokens)
    if hit:
        return 'SDD 技能脚本: %s' % hit
    hit = match_git(tokens, base)
    if hit:
        return hit
    # 只读命令:单个跑本来就会被 harness 放行,这里补的是链式形态
    if exe in READONLY_COMMANDS:
        if exe in READONLY_FILE_COMMANDS and not path_args_inside(tokens, base):
            return None
        return '只读命令: %s' % exe
    hit = match_extra(raw if raw is not None else ' '.join(tokens), tokens, extra)
    if hit:
        return '额外白名单: %s' % hit
    return None


def evaluate(command, extra=None):
    """判定一条命令是否放行。命中返回理由,否则返回 None。

    **复合命令按 shell 习惯拆成若干段,每段都要单独通过判定,全过才整体放行。**
    这样 `cd X && pytest`、`sleep 5 && cd X && pytest`、`cat f | head -20`
    这类常见形态能跑,而 `python x.py && rm -rf /` 仍然被拒(rm 那一段过不了)。

    仍然整条不放行的:

    - **命令替换**(`$( )` 与反引号)—— 在整条命令上查,双引号里照样会被展开
    - **文件重定向**(``>`` ``>>`` ``<``)—— 能写文件
    - **后台运行**(单个 ``&``)—— 脱离监督
    - **引号没闭合** —— shell 会报语法错,一律不放行
    """
    if not isinstance(command, str) or not command.strip():
        return None
    # 命令替换要在**拆段之前**查:藏在 cd 参数里的 $( ) / 反引号
    if has_substitution(command):
        return None
    segments, raws, writes, unterminated = split_segments(command)
    if not segments or writes or unterminated:
        return None
    if extra is None:
        extra, _, _ = load_allowlist()
    reasons = []
    base = os.getcwd()  # 链里 cd 之后的当前目录
    for tokens, raw in zip(segments, raws):
        if tokens and tokens[0].lower() == 'cd':
            # 只是换目录 —— 无害,但要记住去了哪,后面的路径按它解析
            base = resolve_dir(tokens[1] if len(tokens) > 1 else '', base)
            continue
        reason = evaluate_segment(tokens, extra, base, raw)
        if not reason:
            return None  # 有一段过不了 → 整条不放行
        reasons.append(reason)
    if not reasons:
        return None
    return ' + '.join(reasons)


def emit_allow(reason):
    """输出 allow 决定。

    **必须是纯 ASCII**:Windows 上 Python 的 stdout 默认编码是 GBK(cp936),
    之前用 ensure_ascii=False 时中文理由被写成 GBK 字节,harness 按 UTF-8 读
    就是乱码 —— 实测日志里出现过「Hook output does not start with {」。
    ensure_ascii=True 把中文转成 \\uXXXX,任何编码下都是同一串 ASCII 字节。
    """
    out = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'allow',
            'permissionDecisionReason': reason,
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=True))


def audit(reason):
    if not os.environ.get('SDD_HOOK_LOG'):
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), AUDIT_FILE)
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(reason.replace('\n', ' ') + '\n')
    except Exception:
        pass


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ''
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    tool_name = payload.get('tool_name') or ''
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict):
        tool_input = {}

    extra, tools, write_paths = load_allowlist()

    if tool_name != 'Bash':
        reason = evaluate_tool(tool_name, tool_input, tools, None, write_paths)
        if reason:
            emit_allow(reason)
            audit(reason)
        return 0

    command = tool_input.get('command') or ''
    reason = evaluate(command, extra)
    if reason:
        emit_allow(reason)
        audit(reason)
    return 0


if __name__ == '__main__':
    sys.exit(main())
