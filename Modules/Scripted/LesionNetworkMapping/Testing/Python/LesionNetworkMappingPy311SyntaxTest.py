"""The slicer-lnm wheel declares requires-python ">=3.11": every module of LesionNetworkMappingLib must compile on
Python 3.11, although Slicer itself bundles a newer Python.

Python 3.12 (PEP 701) accepts f-strings that 3.11 rejects: a replacement field that reuses the enclosing quote,
contains a backslash or a comment, or (in a single-quoted f-string) spans lines. One such line in report.py made
the report stage of every group run on 3.11 fail with "SyntaxError: f-string: expecting '}'" and write no
report.html. This test finds those constructs with the tokenizer of the running Python, and also compiles every
module with a Python 3.11 interpreter when one is installed.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingPy311SyntaxTest.py -v
"""

import glob
import io
import os
import re
import shutil
import subprocess
import tokenize
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(MODULE_DIR, "LesionNetworkMappingLib")

_PREFIX = re.compile(r"^[A-Za-z]*")


def _quote(token_text):
    """The opening quote ("'", '"', "'''" or '\"\"\"') of a string or f-string start token."""
    body = _PREFIX.sub("", token_text, count=1)
    return body[:3] if body[:3] in ("'''", '"""') else body[:1]


def py311_fstring_problems(source):
    """(line, message) for every f-string construct that needs Python 3.12 or newer (PEP 701)."""
    if not hasattr(tokenize, "FSTRING_START"):
        return []  # Python 3.11 or older: compile() of the module is the check
    problems = []
    stack = []  # opening quotes of the f-strings that enclose the current token
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.FSTRING_END:
            stack.pop()
            continue
        if stack and tok.type in (tokenize.STRING, tokenize.FSTRING_START):
            # Any string token inside an f-string is in a replacement field (literal parts are FSTRING_MIDDLE).
            inner = _quote(tok.string)
            for outer in stack:
                if inner[0] == outer[0] and (len(outer) == 1 or len(inner) == 3):
                    problems.append((tok.start[0], f"replacement field reuses the enclosing quote {outer}"))
                    break
            if tok.type == tokenize.STRING and "\\" in tok.string:
                problems.append((tok.start[0], "backslash inside a replacement field"))
        elif stack and tok.type == tokenize.COMMENT:
            problems.append((tok.start[0], "comment inside a replacement field"))
        elif stack and tok.type in (tokenize.NL, tokenize.NEWLINE) and len(stack[-1]) == 1:
            problems.append((tok.start[0], "line break inside a single-quoted f-string"))
        if tok.type == tokenize.FSTRING_START:
            stack.append(_quote(tok.string))
    return problems


class LesionNetworkMappingPy311SyntaxTest(unittest.TestCase):

    def library_files(self):
        files = sorted(glob.glob(os.path.join(LIB_DIR, "*.py")))
        self.assertGreater(len(files), 10)
        return files

    def test_detector_finds_pep701_fstrings(self):
        if not hasattr(tokenize, "FSTRING_START"):
            self.skipTest("this Python tokenizes f-strings as plain strings")
        self.assertTrue(py311_fstring_problems("x = f'{n} {'a' if n == 1 else 'b'}'\n"))
        self.assertTrue(py311_fstring_problems('x = f"{"a"}"\n'))
        self.assertTrue(py311_fstring_problems("x = f'{chr(10).join(\"\\n\")}'\n"))
        self.assertEqual(py311_fstring_problems("x = f'{n} {\"a\" if n == 1 else \"b\"}'\n"), [])
        self.assertEqual(py311_fstring_problems("x = f'''{\"a\"} {'b'}'''\n"), [])
        self.assertEqual(py311_fstring_problems("x = f'<a{\" open\" if y else \"\"}>'\n"), [])

    def test_library_has_no_python312_only_fstrings(self):
        found = []
        for path in self.library_files():
            with open(path, encoding="utf-8") as f:
                source = f.read()
            for line, message in py311_fstring_problems(source):
                found.append(f"{os.path.basename(path)}:{line}: {message}")
        self.assertEqual(found, [], "constructs that do not compile on Python 3.11 (pyproject requires-python >=3.11)")

    def test_library_compiles_with_python311(self):
        python311 = shutil.which("python3.11") or ("/usr/bin/python3.11" if os.path.exists("/usr/bin/python3.11") else None)
        if not python311:
            self.skipTest("no Python 3.11 interpreter installed")
        code = ("import sys\n"
                "bad = []\n"
                "for p in sys.argv[1:]:\n"
                "    try:\n"
                "        compile(open(p, encoding='utf-8').read(), p, 'exec')\n"
                "    except SyntaxError as e:\n"
                "        bad.append(f'{p}:{e.lineno}: {e.msg}')\n"
                "print('\\n'.join(bad))\n"
                "sys.exit(1 if bad else 0)\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        result = subprocess.run([python311, "-I", "-c", code, *self.library_files()], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
