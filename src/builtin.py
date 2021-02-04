import os
from os import linesep as newline
import re

from agent import quit_command_patterns

COMMAND_ACTION_MAPPING = {
    r"DEFAULT1": 'SEND',
    r"^CTRL.C$" : 'INTR',
    r"^RUN.SEQUENCE(.WAIT)?$": 'NEW_WORKER',
    r"|".join(quit_command_patterns): 'QUIT',
    r"^CLOSE$": 'CLOSE',
    r"^SEND.PULSE$": 'PULSE',
    r"^END.PULSE$": 'INTR',
    r"^WAIT$": 'WAIT',
    r"^SET.PROMPT$": 'SET_PROMPT',
    r"^SEND.ENTER$": 'ENTER',
    r"^FIND$": 'FIND',
    r"^MONITOR$": 'MONITOR',
    r"^SUBSEQUENCE$": 'SUBSEQUENCE',
    r"^END.SUBSEQUENCE$": 'SUBSEQUENCE',
    r"^LOOP$": 'LOOP',
}


class BuiltinCommand(object):
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
    
    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            return None
        except:
            raise
    
    def __setitem__(self, key, value):
        setattr(self, key, value)
    
    @property
    def cmd_dict(self):
        return self.__dict__
    
    def __repr__(self):
        clsname = type(self).__name__
        rpr = self.command
        if self.builtin:
            rpr = 'BUILTIN COMMAND: ' + rpr
        elif self.action == 'CONNECT':
            append = 'login info: ('
            login_info = None
            if hasattr(self, 'user') and self.user:
                login_info = '%r, ' %(self.user)
            if hasattr(self, 'password') and self.password:
                login_info = login_info + '%r' %(self.password)
            append = append + (login_info if login_info else 'None') + ')'
            if hasattr(self, 'boot_expect') and self.boot_expect:
                append = append + ', boot expect: %s' %(self.boot_expect)
            if hasattr(self, 'boot_escape') and self.boot_escape:
                append = append + ', boot escape: %s' %(self.boot_escape)
            rpr = '%s, %s' %(rpr, append)
            timeout = ', timeout: %r' %(self.timeout) if hasattr(self, 'timeout') and self.timeout else ''
            rpr = rpr + timeout
        elif self.action == 'SEND':
            expect = self.expect if self.expect else 'PROMPT'
            escape = self.escape if self.escape else 'none'
            timeout = self.timeout if self.timeout else 'INSTANT'
            rpr = '%s, expect: %r, escape: %r, timeout: %r' %(rpr, expect, escape, timeout)

        return rpr


def match_builtin_command(word):
    found = None
    for command, action in COMMAND_ACTION_MAPPING.items():
        if re.search(command, word):
            found = action

    return found
