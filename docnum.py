#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docnum.py — 文档章节多级编号 / 交叉引用自动维护工具（纯 Python 标准库，单文件）

行流格式（逐行读入，四种行）：
  标题行   #{1,9} + 空格 + [旧编号] + 标题       例: "## 1.2 范围" 或 "## 范围"
           层级用 # 个数表示（自界定、不怕 Tab/空格混用、与 Markdown 一致）
  正文行   其他非空行，归属于最近标题；其中 [[x.y.z]] 为交叉引用（引用旧编号）
  操作行   !insert <父编号|/> [@旧编号] <标题...>   在父节点末尾插入子节点
           !delete <编号>                          删除节点及其子树
           !move <编号> <新父编号|/> [位置]         移动子树，位置为 1 起始，缺省末尾
  空行     忽略

编号规则：规范编号按树位置即时分配（1、1.1、1.1.2 ...）；标题上的旧编号仅作为
交叉引用与操作的定位锚点。每条操作应用后立即全量重排（DFS，O(n)，正确性优先）。

报告：编号冲突（报路径）、编号跳跃、引用不存在的编号（报引用所在行）、
操作引用不存在的节点（报操作行）、结构未闭合（报未闭合分支起始行）。

输出：重排后大纲（正文引用已改写为新编号）、引用映射（旧->新）、错误清单。

用法：python3 docnum.py [文件]     （缺省读 stdin）
      python3 docnum.py --demo     运行内置示例
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

HEADING_RE = re.compile(r'^(#{1,9})\s+(?:(\d+(?:\.\d+)*)\s+)?(\S.*)$')
REF_RE = re.compile(r'\[\[(\d+(?:\.\d+)*)\]\]')
OP_RE = re.compile(r'^!(insert|delete|move)\s*(.*)$')
NUM_RE = re.compile(r'^\d+(?:\.\d+)*$')


def parse_num(text):
    return tuple(int(p) for p in text.split('.'))


def fmt_num(num):
    return '.'.join(str(x) for x in num)


@dataclass
class Node:
    level: int
    title: str
    explicit: tuple | None   # 标题上写的旧编号（引用锚点），可为 None
    line_no: int
    parent: 'Node | None' = None
    children: list = field(default_factory=list)
    body: list = field(default_factory=list)   # [(行号, 文本)]
    num: tuple = ()            # 当前规范编号


@dataclass
class Error:
    kind: str      # conflict / jump / dangling-ref / bad-op / unclosed
    line_no: int
    message: str


class Doc:
    def __init__(self):
        self.root = Node(0, '<root>', None, 0)
        self.errors = []
        self.ops = []          # [(行号, 操作名, 参数串)]

    def error(self, kind, line_no, message):
        self.errors.append(Error(kind, line_no, message))


def node_path(node):
    parts = []
    while node is not None and node.parent is not None:
        parts.append(node.title)
        node = node.parent
    return ' / '.join(reversed(parts))


def iter_nodes(doc):
    def walk(node):
        for child in node.children:
            yield child
            yield from walk(child)
    yield from walk(doc.root)


# ---------------------------------------------------------------- 解析

def parse(text):
    doc = Doc()
    stack = [doc.root]
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        op = OP_RE.match(line)
        if op:
            doc.ops.append((line_no, op.group(1), op.group(2).strip()))
            continue
        head = HEADING_RE.match(line)
        if head:
            level = len(head.group(1))
            explicit = parse_num(head.group(2)) if head.group(2) else None
            node = Node(level, head.group(3).strip(), explicit, line_no)
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1]
            if parent.level < level - 1:
                where = (f'第 {parent.line_no} 行「{parent.title}」'
                         if parent is not doc.root else '文档开头')
                doc.error(
                    'unclosed', line_no,
                    f'结构未闭合：本行级别为 {level}，但自 {where}（级别 '
                    f'{parent.level}）起缺少级别 {parent.level + 1} 的标题')
            node.parent = parent
            parent.children.append(node)
            stack.append(node)
        else:
            stack[-1].body.append((line_no, line))
    return doc


def check_conflicts(doc):
    by_num = {}
    for node in iter_nodes(doc):
        if node.explicit:
            by_num.setdefault(node.explicit, []).append(node)
    for num, nodes in by_num.items():
        if len(nodes) > 1:
            for node in nodes:
                doc.error('conflict', node.line_no,
                          f'编号冲突：{fmt_num(num)} 被重复使用，路径：'
                          f'{node_path(node)}')


def check_jumps(doc):
    def walk(node):
        prev = None
        for child in node.children:
            if child.explicit:
                if prev is None:
                    if child.explicit[-1] != 1:
                        doc.error('jump', child.line_no,
                                  f'编号跳跃：同级首个编号应从 1 开始，'
                                  f'实际为 {fmt_num(child.explicit)}')
                elif (child.explicit[:-1] == prev[:-1]
                      and child.explicit[-1] - prev[-1] > 1):
                    doc.error('jump', child.line_no,
                              f'编号跳跃：{fmt_num(prev)} 之后直接出现 '
                              f'{fmt_num(child.explicit)}')
                prev = child.explicit
            walk(child)
    walk(doc.root)


# ---------------------------------------------------------------- 编号

def renumber(doc):
    def walk(node, prefix):
        for index, child in enumerate(node.children, 1):
            child.num = prefix + (index,)
            walk(child, child.num)
    walk(doc.root, ())


def find_node(doc, num):
    """先按旧编号（引用锚点）找，再按当前规范编号找。"""
    nodes = list(iter_nodes(doc))
    for node in nodes:
        if node.explicit == num:
            return node
    for node in nodes:
        if node.num == num:
            return node
    return None


# ---------------------------------------------------------------- 操作

def apply_op(doc, line_no, op, args):
    if op == 'delete':
        if not NUM_RE.match(args):
            doc.error('bad-op', line_no, f'操作参数非法：!delete {args}')
            return
        node = find_node(doc, parse_num(args))
        if node is None:
            doc.error('bad-op', line_no,
                      f'操作引用不存在的节点：!delete {args}')
            return
        node.parent.children.remove(node)

    elif op == 'insert':
        parts = args.split(None, 1)
        if not parts:
            doc.error('bad-op', line_no, '操作参数非法：!insert 缺少参数')
            return
        parent = doc.root if parts[0] == '/' else (
            find_node(doc, parse_num(parts[0])) if NUM_RE.match(parts[0]) else None)
        if parent is None:
            doc.error('bad-op', line_no,
                      f'操作引用不存在的节点：!insert {args}')
            return
        rest = parts[1] if len(parts) > 1 else ''
        explicit = None
        if rest.startswith('@'):
            tok, _, rest = rest[1:].partition(' ')
            if NUM_RE.match(tok):
                explicit = parse_num(tok)
        if not rest.strip():
            doc.error('bad-op', line_no, '操作参数非法：!insert 缺少标题')
            return
        node = Node(parent.level + 1, rest.strip(), explicit, line_no,
                    parent=parent)
        parent.children.append(node)

    elif op == 'move':
        parts = args.split()
        if len(parts) < 2 or not NUM_RE.match(parts[0]):
            doc.error('bad-op', line_no, f'操作参数非法：!move {args}')
            return
        node = find_node(doc, parse_num(parts[0]))
        if node is None:
            doc.error('bad-op', line_no,
                      f'操作引用不存在的节点：!move {args}')
            return
        target = parts[1]
        parent = doc.root if target == '/' else (
            find_node(doc, parse_num(target)) if NUM_RE.match(target) else None)
        if parent is None:
            doc.error('bad-op', line_no,
                      f'操作引用不存在的节点：!move {args}（新父节点）')
            return
        up = parent
        while up is not None:
            if up is node:
                doc.error('bad-op', line_no,
                          f'操作非法：!move {args} 会把节点移入自己的子树')
                return
            up = up.parent
        index = None
        if len(parts) > 2:
            if parts[2].isdigit() and int(parts[2]) >= 1:
                index = int(parts[2]) - 1
            else:
                doc.error('bad-op', line_no,
                          f'操作参数非法：!move 位置须为 >=1 的整数')
                return
        node.parent.children.remove(node)
        node.parent = parent
        node.level = parent.level + 1
        if index is None or index > len(parent.children):
            parent.children.append(node)
        else:
            parent.children.insert(index, node)
    renumber(doc)   # 每条操作应用后立即重排受影响层级及后续编号


def fix_levels(doc):
    """移动后子树层级可能失真，按深度重设 level（仅影响显示/后续解析一致性）。"""
    def walk(node, level):
        node.level = level
        for child in node.children:
            walk(child, level + 1)
    for child in doc.root.children:
        walk(child, 1)


# ---------------------------------------------------------------- 引用

def resolve_refs(doc):
    """返回 (映射列表, 引用出现列表)。映射: 旧编号->新编号；出现: (行号, 旧, 新|None)。"""
    explicit_map = {n.explicit: n for n in iter_nodes(doc) if n.explicit}
    mapping = sorted(((old, node.num) for old, node in explicit_map.items()))
    occurrences = []
    for node in iter_nodes(doc):
        for line_no, text in node.body:
            for m in REF_RE.finditer(text):
                old = parse_num(m.group(1))
                target = explicit_map.get(old)
                occurrences.append((line_no, old, target.num if target else None))
                if target is None:
                    doc.error('dangling-ref', line_no,
                              f'引用不存在的编号：[[{fmt_num(old)}]]')
    return mapping, occurrences


def rewrite_refs(text, doc):
    explicit_map = {n.explicit: n for n in iter_nodes(doc) if n.explicit}

    def repl(m):
        old = parse_num(m.group(1))
        target = explicit_map.get(old)
        return f'[[{fmt_num(target.num)}]]' if target else m.group(0)

    return REF_RE.sub(repl, text)


# ---------------------------------------------------------------- 输出

def render(doc, mapping, occurrences, out):
    p = out.write
    p('=== 重排后大纲 ===\n')

    def walk(node):
        num = fmt_num(node.num)
        old = f'（旧编号 {fmt_num(node.explicit)}）' if node.explicit else ''
        p(f'{"  " * (node.level - 1)}{num} {node.title}{old}\n')
        for _, text in node.body:
            p(f'{"  " * node.level}{rewrite_refs(text, doc)}\n')
        for child in node.children:
            walk(child)

    for child in doc.root.children:
        walk(child)

    p('\n=== 引用映射（旧编号 -> 新编号） ===\n')
    if mapping:
        for old, new in mapping:
            p(f'{fmt_num(old)} -> {fmt_num(new)}\n')
    else:
        p('（无显式旧编号）\n')
    if occurrences:
        p('\n引用出现位置：\n')
        for line_no, old, new in occurrences:
            if new is None:
                p(f'第 {line_no} 行：[[{fmt_num(old)}]] -> 失效（目标不存在）\n')
            else:
                p(f'第 {line_no} 行：[[{fmt_num(old)}]] -> [[{fmt_num(new)}]]\n')

    p('\n=== 错误清单 ===\n')
    if doc.errors:
        labels = {'conflict': '编号冲突', 'jump': '编号跳跃',
                  'dangling-ref': '悬空引用', 'bad-op': '非法操作',
                  'unclosed': '结构未闭合'}
        for err in doc.errors:
            p(f'[{labels.get(err.kind, err.kind)}] 第 {err.line_no} 行：'
              f'{err.message}\n')
    else:
        p('（无错误）\n')


def run(text, out):
    doc = parse(text)
    check_conflicts(doc)
    check_jumps(doc)
    renumber(doc)
    for line_no, op, args in doc.ops:
        apply_op(doc, line_no, op, args)
    fix_levels(doc)
    renumber(doc)
    mapping, occurrences = resolve_refs(doc)
    render(doc, mapping, occurrences, out)


DEMO = """\
# 1 总则
本文参见 [[1.2]]、[[3.1]] 与 [[9.9]]。
## 1.1 目的
## 1.2 范围
## 1.2 重复编号
# 2 安装
### 2.1.1 深入未闭合
# 3 使用
## 3.1 快速上手
## 3.3 跳过 3.2
!insert 1 新增节
!move 3.1 2
!delete 3.3
!delete 7.7
"""


def main(argv):
    if '--demo' in argv:
        text = DEMO
    elif len(argv) > 1:
        with open(argv[1], encoding='utf-8') as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    run(text, sys.stdout)


if __name__ == '__main__':
    main(sys.argv[1:])
