import os
import sys
from os import linesep as newline
import datetime
import re
import subprocess
from subprocess import Popen, PIPE


# Customized Exceptions
class SequenceError(Exception): pass
'''Test sequence related errors'''

class CommandError(Exception): pass
'''Command ralated errors, eg, command not found.'''

class PtyProcessError(Exception): pass
"""Generic error class for this package."""

class FileError(Exception): pass
"""File related errors, eg, logfile error."""

class RecoveryError(Exception): pass
"""Recover failed after retry."""

class ContextError(Exception):
    '''Nested connections related errors, we called it Context.'''
    def __init__(self, *args, **kw):
        super(ContextError, self).__init__(*args)
        self.prompt = kw.get('prompt')
        self.output = kw.get('output')

    def __repr__(self):
        rpr = 'ContextError: '
        rpr = rpr + self.args[0] + newline
        if self.prompt:
            rpr = rpr + 'SHELL PROMPT:' + newline + self.prompt + newline + newline
        if self.output:
            rpr = rpr + 'READ OUTPUT:' + newline + self.output + newline + newline

class ExpectError(Exception):
    '''Expect action related errors, eg, expected target not found.'''
    def __init__(self, *args, **kw):
        super(ExpectError, self).__init__(*args)
        self.prompt = kw.get('prompt')
        self.output = kw.get('output')

    def __repr__(self):
        rpr = 'ExpectError: '
        rpr = rpr + self.args[0] + newline
        if self.prompt:
            rpr = rpr + 'SHELL PROMPT:' + newline + self.prompt + newline + newline
        if self.output:
            rpr = rpr + 'READ OUTPUT:' + newline + self.output + newline + newline
        return rpr

class TimeoutError(Exception):
    """Command timeout errors."""
    def __init__(self, *args, **kw):
        super(TimeoutError, self).__init__(*args)
        self.prompt = kw.get('prompt')
        self.output = kw.get('output')

    def __repr__(self):
        rpr = 'TimeoutError: '
        rpr = rpr + self.args[0] + newline
        if self.prompt:
            rpr = rpr + 'SHELL PROMPT:' + newline + self.prompt + newline + newline
        if self.output:
            rpr = rpr + 'READ OUTPUT:' + newline + self.output + newline + newline
        return rpr

PY3 = sys.version_info[0] >= 3
if PY3:
    def _bytes(text, encoding="utf-8"):
        return bytes(text, encoding=encoding) if isinstance(text, str) else bytes(str(text), encoding=encoding)

    def _str(text, encoding="utf-8", errors="ignore"):
        return text.decode(encoding, errors=errors) if isinstance(text, bytes) else str(text)

else:
    def _str(text, encoding='utf-8'):
        return text.encode(encoding) if isinstance(text, unicode) else str(text)

    def _bytes(text):
        return text if isinstance(text, str) else bytes(text)


# 7-bit C1 ANSI sequences
ANSI_ESCAPES = re.compile(r'''
    \x1B  # ESC
    (?:   # 7-bit C1 Fe (except CSI)
        [@-Z\\-_]
    |     # or [ for CSI, followed by a control sequence
        \[
        [0-?]*  # Parameter bytes
        [ -/]*  # Intermediate bytes
        [@-~]   # Final byte
    )
''', re.VERBOSE)

try:
    from shutil import which  # Python >= 3.3
except ImportError:
    import os, sys
    
    # This is copied from Python 3.4.1
    def which(cmd, mode=os.F_OK | os.X_OK, path=None):
        """Given a command, mode, and a PATH string, return the path which
        conforms to the given mode on the PATH, or None if there is no such
        file.
    
        `mode` defaults to os.F_OK | os.X_OK. `path` defaults to the result
        of os.environ.get("PATH"), or can be overridden with a custom search
        path.
    
        """
        # Check that a given file can be accessed with the correct mode.
        # Additionally check that `file` is not a directory, as on Windows
        # directories pass the os.access check.
        def _access_check(fn, mode):
            return (os.path.exists(fn) and os.access(fn, mode)
                    and not os.path.isdir(fn))
    
        # If we're given a path with a directory part, look it up directly rather
        # than referring to PATH directories. This includes checking relative to the
        # current directory, e.g. ./script
        if os.path.dirname(cmd):
            if _access_check(cmd, mode):
                return cmd
            return None
    
        if path is None:
            path = os.environ.get("PATH", os.defpath)
        if not path:
            return None
        path = path.split(os.pathsep)
    
        if sys.platform == "win32":
            # The current directory takes precedence on Windows.
            if not os.curdir in path:
                path.insert(0, os.curdir)
    
            # PATHEXT is necessary to check on Windows.
            pathext = os.environ.get("PATHEXT", "").split(os.pathsep)
            # See if the given file matches any of the expected path extensions.
            # This will allow us to short circuit when given "python.exe".
            # If it does match, only test that one, otherwise we have to try
            # others.
            if any(cmd.lower().endswith(ext.lower()) for ext in pathext):
                files = [cmd]
            else:
                files = [cmd + ext for ext in pathext]
        else:
            # On other platforms you don't have things like PATHEXT to tell you
            # what file suffixes are executable, so just pass on cmd as-is.
            files = [cmd]
    
        seen = set()
        for dir in path:
            normdir = os.path.normcase(dir)
            if not normdir in seen:
                seen.add(normdir)
                for thefile in files:
                    name = os.path.join(dir, thefile)
                    if _access_check(name, mode):
                        return name
        return None


def get_prompt_line(out):
    out = _str(out)
    
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    last_line = lines[-1] if lines else ''
    
    return last_line


def split_out_lines(out):
    out = _str(out)
    
    lines = [line for line in out.splitlines() if line.strip()]
    
    return lines if lines else None


def split_command_args(command):
    command = _str(command)
    
    return [x.strip() for x in command.split(' ') if x.strip()]


def new_log_path(sequence='', suffix=''):
    now = datetime.datetime.now().strftime('%b-%d-%H%M-%G')
    if not sequence: sequence = 'unknown'
    sequence = sequence.split('.')[0]

    if in_search('failure', suffix): base = './log/failure'
    elif in_search('errordump', suffix): base = './log/errordump'
    else: base = './log'

    if suffix: logpath = '%s/%s_%s_%s.log' %(base, now, sequence, suffix)
    else: logpath = '%s/%s_%s.log' %(base, now, sequence)

    return logpath


def parse_time_to_sec(t):
    rates = {
        'h': 3600,
        'm': 60,
        's': 1,
        }

    t = t.strip().lower()
    t_list = re.findall(r"[^\W\d_]|\d*\.?\d+", t)

    t_len = len(t_list)
    t_index = 1
    seconds = 0.0
    while t_index < t_len:
        if t_list[t_index].isalpha():
            try:
                coeffi = float(t_list[t_index-1])
                seconds += coeffi*rates[t_list[t_index][0]]
            except ValueError:
                pass
        t_index += 1

    try:
        sec = float(t_list[-1])
        seconds += sec
    except ValueError:
        pass

    return int(seconds) if seconds > 0 else 0


def strip_ansi_escape(text):
    result = text
    if isinstance(text, type('')):
        result = ANSI_ESCAPES.sub('', text)

    return result


def prompt_strip_date(prompt_read):
    regex = r"[A-Za-z]{3} [A-Za-z]{3} \d{2} \d{2}:\d{2}:\d{2} "
    prompt_read = _str(prompt_read)
    fixed_prompt = prompt_read

    match = re.search(regex, prompt_read)
    if match is not None:
        fixed_prompt = fixed_prompt[match.end():]

    return fixed_prompt


def ucs_fuzzy_match(p, s):
    p = p.lstrip(' ')
    s = s.lstrip(' ')
    if p and '\n' not in p and s:
        if s.startswith(p): return s[len(p):]
        # serail console will flush a '\r' when one line console buffer is overflowed
        if p.find('\r') > 0:
            parts = p.split('\r')
            left = parts[0]
            right = parts[1]
            rlen = len(right)
            if s.startswith(left) and rlen >= 3:
                rsub = s[len(left):]
                rpos = rsub.find(right)
                if rpos > -1: return rsub[rpos+rlen:]
                cursor = 1
                while cursor <= rlen - 2:
                    rleft = right[:cursor]
                    rright = right[cursor:]
                    if rsub.startswith(rright) and left[::-1].startswith(rleft[::-1]):
                        return rsub[len(rright):]
                    cursor += 1
    return ''


def ucs_dupsubstr_verify(s):
    s = s.strip(' ')
    if s and len(s) >= 4:
        pos = len(s)//2 - 2
        mid = (len(s)-1)//2
        left = s[:pos].strip(' ')
        right = s[pos:].strip(' ')
        while -3 <= (pos - mid) <= 3:
            if left and right and left == right:
                return True
            pos += 1
            left = s[:pos].strip(' ')
            right = s[pos:].strip(' ')

    return False


def in_search(p, s, do_find=False):
    if do_find:
        pos = s.find(p)
        if pos < 0:
            match = re.search(p, s, re.M)
            if match is not None:
                pos = match.start()
        return pos
    else:
        return (p in s) or (re.search(p, s, re.M | re.I) is not None)


def local_run_cmd(cmd, timeout=None):
    with Popen(cmd.strip(), stdout=PIPE, stderr=PIPE, shell=True, close_fds=(os.name=='posix')) as process:
        timeout = timeout if timeout and timeout > 0 else None
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            if subprocess._mswindows:
                exc.stdout, exc.stderr = process.communicate()
            else:
                process.wait()
            raise TimeoutError(msg='Command exceeded time limit: %rsec' %(timeout))
        except:
            process.kill()
            raise

    return _str(stdout), _str(stderr)
