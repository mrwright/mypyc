from mypyc.ops import format_func, Assign, LoadInt, Unbox, Box, Register, ModuleIR, Cast, RegisterOp
import mypyc.analysis as analysis
from typing import List, Tuple, Dict, Iterable, Set, TypeVar, Optional


from collections import defaultdict

def dce_blocks(blocks):
    """Simple dead code elimination pass, for a single function.
    """
    counts = defaultdict(int)
    locs = defaultdict(set)
    active = set()
    for b, block in enumerate(blocks):
        for i in range(len(block.ops)):
            op = block.ops[i]
            for src in op.sources():
                counts[src] += 1
                locs[src].add((b, i))
            active.add((b, i))

    while active:
        b, i = next(iter(active))
        active.remove((b, i))
        op = blocks[b].ops[i]

        if isinstance(op, RegisterOp) and op.safe_to_optimize_out() and counts[op] == 0:
            for src in op.sources():
                counts[src] -= 1
            active.update(locs[op])
            blocks[b].ops[i] = None

    for block in blocks:
        block.ops = [op for op in block.ops if op is not None]

def dce(modules: List[Tuple[str, ModuleIR]]):
    # TODO: iterate over these.
    ir = modules[0][1]
    func_ir = ir.functions[0]
    fn = func_ir
    dce_blocks(fn.blocks)
    print("\n".join(format_func(func_ir)))
