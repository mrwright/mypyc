"""Code for creating Python C extension modules from IR (intermediate representation)."""

import textwrap
from typing import List

from mypyc.emitter import (
    native_function_header,
    generate_c_for_function,
    wrapper_function_header,
    generate_wrapper_function
)
from mypyc.common import PREFIX
from mypyc.ops import FuncIR


def generate_c_module(name: str, fns: List[FuncIR]) -> str:
    result = []
    result.append('#include <Python.h>')
    result.append('#include <CPy.h>')
    result.append('')

    for fn in fns:
        fragments = generate_function_declaration(fn)
        result.extend(fragments)

    result.append('')

    fragments = generate_module_def(name, fns)
    result.extend(fragments)

    for fn in fns:
        result.append('')
        fragments = generate_c_for_function(fn)
        result.extend([frag.rstrip() for frag in fragments])
        result.append('')
        fragments = generate_wrapper_function(fn)
        result.extend(fragments)

    return '\n'.join(result)


def generate_function_declaration(fn: FuncIR) -> List[str]:
    return [
        '{};'.format(native_function_header(fn)),
        '{};'.format(wrapper_function_header(fn))
    ]


init_func_format = textwrap.dedent("""\
    static struct PyModuleDef module = {{
        PyModuleDef_HEAD_INIT,
        "{name}",
        NULL, /* docstring */
        -1,       /* size of per-interpreter state of the module,
                     or -1 if the module keeps state in global variables. */
        module_methods
    }};

    PyMODINIT_FUNC PyInit_{name}(void)
    {{
        return PyModule_Create(&module);
    }}
""")


def generate_module_def(module_name: str, fns: List[FuncIR]) -> List[str]:
    lines = []
    lines.append('static PyMethodDef module_methods[] = {')
    for fn in fns:
        lines.append(
            ('    {{"{name}", (PyCFunction){prefix}{name}, METH_VARARGS | METH_KEYWORDS, '
             'NULL /* docstring */}},').format(
                name=fn.name,
                prefix=PREFIX))
    lines.append('    {NULL, NULL, 0, NULL}')
    lines.append('};')
    lines.append('')
    lines.extend(init_func_format.format(name=module_name).splitlines())
    return lines