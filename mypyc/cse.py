from mypyc.ops import format_func, Assign, LoadInt, Unbox, Box, Register, ModuleIR, Cast
import mypyc.analysis as analysis
from typing import List, Tuple, Dict, Iterable, Set, TypeVar, Optional


from collections import defaultdict


def dest(o):
    if isinstance(o, Assign):
        return o.__class__.__name__ + " " + o.dest.name + " " + str(o.src)
    else:
        return o.__class__.__name__


class OpEquivalenceWrapper:
    # TODO: this should handle general ops for which safe_to_optimize_out is True,
    # rather than special-casing a small list here.
    def __init__(self, op, generations) -> None:
        self.op = op
        self.generations = generations

    def __eq__(self, other) -> bool:
        print("__eq__", self, other)
        if not isinstance(other, OpEquivalenceWrapper):
            return False

        if self.op.__class__ is not other.op.__class__:
            return False

        if isinstance(self.op, LoadInt):
            return self.op.value == other.op.value

        if isinstance(self.op, Register):
            return (self.op.name == other.op.name and
                    self.generations.get(self.op.name) == other.generations.get(self.op.name))

        if isinstance(self.op, (Unbox, Cast)):
            # TODO: the type matters.
            return (OpEquivalenceWrapper(self.op.src, self.generations) ==
                    OpEquivalenceWrapper(other.op.src, self.generations))

        return False

    def __hash__(self) -> int:
        if isinstance(self.op, LoadInt):
            return hash((self.op.__class__, self.op.value))
        elif isinstance(self.op, Register):
            return hash((self.op.__class__, self.op.name, self.generations.get(self.op.name)))
        elif isinstance(self.op, (Unbox, Cast)):
            # TODO: the type matters.
            return hash((self.op.__class__, OpEquivalenceWrapper(self.op.src, self.generations)))
        else:
            return hash((self.op.__class__, self.op))

    def __str__(self) -> str:
        return "OpEquiv:{}".format(str(self.op))

    def __repr__(self) -> str:
        return "OpEquiv:{}".format(repr(self.op))

def cse_block(block):
    x = {}
    generations = defaultdict(int)
    for i in range(len(block.ops)):
        op = block.ops[i]
        print(generations, op)
        oew = OpEquivalenceWrapper(op, dict(generations))
        if oew not in x:
            x[oew] = oew
        if isinstance(op, (Assign, Unbox, Cast)):
            src_oew = OpEquivalenceWrapper(op.src, dict(generations))
            op.src = x.get(src_oew, src_oew).op

        # Increment generation count for the target of this op, if appropriate.
        if isinstance(op, Assign):
            generations[op.dest.name] += 1

    print(x)

def cse(modules: List[Tuple[str, ModuleIR]]):
    ir = modules[0][1]
    fn = ir.functions[0]
    print(len(fn.blocks))
    for i, block in enumerate(fn.blocks):
        print("CSE block {}".format(i))
        cse_block(block)
    print("\n".join(format_func(fn)))
