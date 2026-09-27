#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
renum.py — 章节多级编号重排与交叉引用同步工具（纯 Python 标准库，单文件）

用法:
    python3 renum.py 输入文件        # 或: python3 renum.py - < 输入文件（ stdin ）

========================================================================
输入格式（行流；空行与 '#' 开头的注释行被忽略）
========================================================================
结构阶段（分隔行 '---' 之前），层级用显式 sec/end 配对标记表示：

    sec [旧编号] 标题文字，可含 [[1.2.3]] 形式的交叉引用
    end                          # 关闭最近打开、尚未关闭的 sec

操作阶段（'---' 之后），逐条应用，节点一律用“旧编号”指代：

    delete <旧编号>
    move   <旧编号> <新父旧编号|ROOT> <插入下标>
    insert <父旧编号|ROOT> <插入下标> [新节点旧编号] 标题文字

说明：
* 节点的“旧编号”即它在本次重排前的编号：若 sec/insert 行显式给出编号则
  采用之，否则按流入顺序即时分配位置编号（1、1.1、1.1.2 …）。
* 交叉引用 [[x.y.z]] 引用的是旧编号；重排后工具输出新编号树、
  旧→新编号映射、引用映射（并把标题中的引用改写为新编号）以及错误清单。
* 报告的错误：编号冲突（含路径）、编号跳跃、引用不存在的编号（含引用
  所在行号）、操作引用不存在的节点、结构未闭合（含起始行号）等。

示例输入见文件末尾 SAMPLE 常量，可用 `python3 renum.py --demo` 直接运行。
"""

import re
import sys

NUM_FULL_RE = re.compile(r"\d+(?:\.\d+)*\Z")
REF_RE = re.compile(r"\[\[(\d+(?:\.\d+)*)\]\]")
ROOT = "ROOT"


class Node:
    __slots__ = ("old", "declared", "title", "line", "parent", "children",
                 "new", "new_title", "deleted", "refs")

    def __init__(self, old, declared, title, line, parent):
        self.old = old            # 旧编号（重排前的身份标识）
        self.declared = declared  # 旧编号是否为输入显式声明
        self.title = title
        self.line = line          # 定义所在行号
        self.parent = parent
        self.children = []
        self.new = ""             # 重排后的新编号
        self.new_title = title    # 引用改写后的标题
        self.deleted = False
        self.refs = []            # 本节点标题中出现的 Ref 列表


class Ref:
    __slots__ = ("line", "owner", "old_target", "target", "new_target")

    def __init__(self, line, owner, old_target):
        self.line = line              # 引用出现位置的行号
        self.owner = owner            # 引用所在节点
        self.old_target = old_target  # 引用写的旧编号
        self.target = None            # 解析到的 Node
        self.new_target = None        # 重排后对应的新编号


def subtree(node):
    yield node
    for child in node.children:
        yield from subtree(child)


class Document:
    def __init__(self):
        self.roots = []
        self.nodes = []     # 出现过的全部节点（含已删除）
        self.refs = []
        self.errors = []
        self.by_old = {}    # 旧编号 -> 存活节点（冲突时先到先得，冲突另报）
        self.ops = []       # (行号, 操作行文本)

    # ---------------- 路径显示 ----------------
    def path(self, node):
        parts = []
        cur = node
        while cur is not None:
            parts.append(cur.title or "（无标题）")
            cur = cur.parent
        return " / ".join(reversed(parts)) + "（第{}行）".format(node.line)

    # ---------------- 结构阶段 ----------------
    def parse(self, text):
        stack = []
        phase = "struct"
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line == "---":
                if phase != "struct":
                    self.errors.append("第{}行: 重复的 '---' 分隔行".format(lineno))
                phase = "ops"
                continue
            if phase == "struct":
                if line == "end":
                    if not stack:
                        self.errors.append("第{}行: 'end' 没有匹配的 'sec'".format(lineno))
                    else:
                        stack.pop()
                    continue
                head, _, rest = line.partition(" ")
                if head == "sec":
                    self._open_node(rest.strip(), lineno, stack)
                    continue
                self.errors.append("第{}行: 无法识别的行: {}".format(lineno, line))
            else:
                self.ops.append((lineno, line))
        # 结构未闭合：报告每个未闭合 sec 的起始位置
        for node in stack:
            self.errors.append("结构未闭合: 起始于第{}行的 sec（标题: {!r}）".format(
                node.line, node.title))

    def _open_node(self, rest, lineno, stack):
        parent = stack[-1] if stack else None
        first, _, tail = rest.partition(" ")
        if first and NUM_FULL_RE.match(first):
            old, declared, title = first, True, tail.strip()
        else:
            # 即时分配：按流入顺序在父节点下取位置编号
            idx = len(parent.children) if parent else len(self.roots)
            old = "{}.{}".format(parent.old, idx + 1) if parent else str(idx + 1)
            declared, title = False, rest
        node = Node(old, declared, title, lineno, parent)
        (parent.children if parent else self.roots).append(node)
        self.nodes.append(node)
        self.by_old.setdefault(old, node)
        self._collect_refs(node, lineno)
        stack.append(node)

    def _collect_refs(self, node, lineno):
        for m in REF_RE.finditer(node.title):
            ref = Ref(lineno, node, m.group(1))
            node.refs.append(ref)
            self.refs.append(ref)

    # ---------------- 静态检查：冲突 / 跳跃 ----------------
    def check_conflicts(self):
        seen = {}
        for node in self.nodes:
            seen.setdefault(node.old, []).append(node)
        for num, holders in sorted(seen.items()):
            if len(holders) > 1:
                where = "；".join(self.path(n) for n in holders)
                self.errors.append("编号冲突: 编号 {} 同时用于 {}".format(num, where))

    def check_jumps(self):
        def scan(children, parent):
            prefix = parent.old + "." if parent else ""
            got = set()
            for c in children:
                if prefix and not c.old.startswith(prefix):
                    continue
                if not prefix and "." in c.old:
                    continue
                last = c.old[len(prefix):]
                if last.isdigit():
                    got.add(int(last))
            for k in range(1, max(got) + 1 if got else 1):
                if k not in got:
                    where = ("节点 {}（{}）".format(parent.old, parent.title)
                             if parent else "顶层")
                    self.errors.append("编号跳跃: {}下缺少编号 {}{}".format(
                        where, prefix, k))
        def walk(node):
            scan(node.children, node)
            for c in node.children:
                walk(c)
        scan(self.roots, None)
        for r in self.roots:
            walk(r)

    # ---------------- 引用解析（结构阶段结束后、操作前） ----------------
    def resolve_refs(self):
        for ref in self.refs:
            if ref.target is None:
                self._resolve_one(ref)

    def _resolve_one(self, ref):
        target = self.by_old.get(ref.old_target)
        if target is None or target.deleted:
            self.errors.append(
                "第{}行: 引用不存在的编号 [[{}]]（位于 {}）".format(
                    ref.line, ref.old_target, self.path(ref.owner)))
        else:
            ref.target = target

    # ---------------- 操作阶段 ----------------
    def apply_ops(self):
        for lineno, line in self.ops:
            parts = line.split()
            cmd = parts[0]
            if cmd == "delete" and len(parts) == 2:
                self._op_delete(parts[1], lineno)
            elif cmd == "move" and len(parts) == 4:
                self._op_move(parts[1], parts[2], parts[3], lineno)
            elif cmd == "insert" and len(parts) >= 4:
                self._op_insert(parts[1], parts[2], parts[3:], lineno)
            else:
                self.errors.append("第{}行: 无法识别的操作: {}".format(lineno, line))

    def _lookup(self, num, lineno, opname):
        node = self.by_old.get(num)
        if node is None or node.deleted:
            self.errors.append(
                "第{}行: {} 操作引用不存在的节点 {}".format(lineno, opname, num))
            return None
        return node

    def _op_delete(self, num, lineno):
        node = self._lookup(num, lineno, "delete")
        if node is None:
            return
        for n in subtree(node):
            n.deleted = True
            if self.by_old.get(n.old) is n:
                del self.by_old[n.old]
        (node.parent.children if node.parent else self.roots).remove(node)

    def _op_move(self, num, parent_num, idx_s, lineno):
        node = self._lookup(num, lineno, "move")
        if parent_num == ROOT:
            new_parent = None
        else:
            new_parent = self._lookup(parent_num, lineno, "move（新父节点）")
        if node is None or (parent_num != ROOT and new_parent is None):
            return
        if not idx_s.lstrip("-").isdigit():
            self.errors.append("第{}行: move 的插入下标不是整数: {}".format(lineno, idx_s))
            return
        cur = new_parent
        while cur is not None:
            if cur is node:
                self.errors.append(
                    "第{}行: 不能将节点 {} 移动到它自己的子树内".format(lineno, num))
                return
            cur = cur.parent
        (node.parent.children if node.parent else self.roots).remove(node)
        siblings = new_parent.children if new_parent else self.roots
        idx = max(0, min(int(idx_s), len(siblings)))
        siblings.insert(idx, node)
        node.parent = new_parent

    def _op_insert(self, parent_num, idx_s, rest, lineno):
        if parent_num == ROOT:
            parent = None
        else:
            parent = self._lookup(parent_num, lineno, "insert（父节点）")
            if parent is None:
                return
        if not idx_s.lstrip("-").isdigit():
            self.errors.append("第{}行: insert 的插入下标不是整数: {}".format(lineno, idx_s))
            return
        declared = False
        old = None
        if NUM_FULL_RE.match(rest[0]):
            old, declared = rest[0], True
            rest = rest[1:]
            clash = self.by_old.get(old)
            if clash is not None and not clash.deleted:
                self.errors.append(
                    "第{}行: 编号冲突: 新插入节点声明的编号 {} 已被 {}".format(
                        lineno, old, self.path(clash)))
        title = " ".join(rest)
        node = Node(old, declared, title, lineno, parent)
        siblings = parent.children if parent else self.roots
        idx = max(0, min(int(idx_s), len(siblings)))
        siblings.insert(idx, node)
        self.nodes.append(node)
        if old is not None:
            self.by_old.setdefault(old, node)
        # 新节点标题中的引用：按当前（操作进行中的）旧编号即时解析
        self._collect_refs(node, lineno)
        for ref in node.refs:
            self._resolve_one(ref)

    # ---------------- 重排与引用同步 ----------------
    def renumber(self):
        def rec(children, prefix):
            for i, c in enumerate(children):
                c.new = "{}.{}".format(prefix, i + 1) if prefix else str(i + 1)
                rec(c.children, c.new)
        rec(self.roots, "")

    def finalize_refs(self):
        for ref in self.refs:
            if ref.owner.deleted:
                continue  # 引用随所在节点一起被删除
            if ref.target is None:
                continue  # 已在解析阶段报告
            if ref.target.deleted:
                self.errors.append(
                    "第{}行: 引用目标 {} 已被删除（位于 {}）".format(
                        ref.line, ref.old_target, self.path(ref.owner)))
            else:
                ref.new_target = ref.target.new
        # 改写存活节点标题中的引用为新编号
        for node in self.nodes:
            if node.deleted:
                continue
            mapping = {r.old_target: r.new_target
                       for r in node.refs if r.new_target}
            if mapping:
                node.new_title = REF_RE.sub(
                    lambda m: "[[{}]]".format(mapping.get(m.group(1), m.group(1))),
                    node.title)

    # ---------------- 输出 ----------------
    def report(self):
        out = ["=" * 60, "重排后的结构（新编号）", "=" * 60]

        def emit(node, depth):
            out.append("  " * depth + "{}  {}".format(node.new, node.new_title))
            for c in node.children:
                emit(c, depth + 1)
        for r in self.roots:
            emit(r, 0)

        out += ["", "=" * 60, "编号映射（旧 → 新）", "=" * 60]
        changed = 0
        for node in self.nodes:
            if node.deleted:
                out.append("{} → （已删除）  {}".format(node.old, node.title))
                changed += 1
            elif node.old is None:
                out.append("（新增）→ {}  {}".format(node.new, node.title))
                changed += 1
            elif node.old != node.new:
                out.append("{} → {}  {}".format(node.old, node.new, node.title))
                changed += 1
        if not changed:
            out.append("（所有编号均未变化）")

        out += ["", "=" * 60, "交叉引用映射（按引用出现位置）", "=" * 60]
        any_ref = False
        for ref in self.refs:
            if ref.owner.deleted or ref.new_target is None:
                continue
            any_ref = True
            mark = "（不变）" if ref.old_target == ref.new_target else ""
            out.append("第{}行: [[{}]] → [[{}]]{} （位于 {} {}）".format(
                ref.line, ref.old_target, ref.new_target, mark,
                ref.owner.new, ref.owner.new_title))
        if not any_ref:
            out.append("（无有效引用）")

        out += ["", "=" * 60, "错误清单", "=" * 60]
        if self.errors:
            out += ("- " + e for e in self.errors)
        else:
            out.append("无错误")
        return "\n".join(out)


SAMPLE = """\
# 示例：结构阶段
sec 1 第一章 总则
sec 1.1 适用范围，参见 [[1.2]] 与 [[9.9]]
end
sec 1.2 术语
end
sec 1.4 跳跃的节
end
end
sec 2 第二章 冲突演示
sec 1.1 重复编号节
end
end
sec 3 第三章 将被删除
end
sec 4 第四章 将被移动到第一章下
end
sec 6 未闭合的章
---
# 示例：操作阶段
delete 3
move 4 1 1
insert ROOT 1 5 插入的新章，引用 [[1.1]]
insert 9 0 父节点不存在
delete 7.7
move 1 1.1 0
"""


def main(argv):
    if len(argv) == 2 and argv[1] == "--demo":
        text = SAMPLE
    elif len(argv) == 2 and argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    elif len(argv) == 2:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    elif len(argv) == 1:
        text = sys.stdin.read()
    else:
        print(__doc__)
        return 2
    doc = Document()
    doc.parse(text)
    doc.check_conflicts()
    doc.check_jumps()
    doc.resolve_refs()
    doc.apply_ops()
    doc.renumber()
    doc.finalize_refs()
    print(doc.report())
    return 1 if doc.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
