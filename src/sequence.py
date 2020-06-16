import os
from os import linesep as newline
import re

from agent import (connect_commands,
                   quit_command_patterns,
                   waitpassphrase_command_pattern)
from const import (seq_comment_header,
                   seq_continue_nextline,
                   seq_item_delimiter,
                   seq_subitem_delimiter,
                   sequence_file_entry)

import utils
from utils import SequenceError


COMMAND_ACTION_MAP = {
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
}


class SequenceCommand(object):
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
        if self.internal:
            rpr = 'INTERNAL COMMAND: ' + rpr
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

# parse expect info
def sequence_expect_parser(expect_info):
    if expect_info:
        expects = [x.strip() for x in expect_info.split(seq_subitem_delimiter) if x.strip()]
    else:
        expects = []

    if not expects: return None

    if len(expects) == 1 and 'PROMPT' in expects: return None

    return expects

# parse escape info
def sequence_escape_parser(escape_info):
    if escape_info:
        escapes = [x.strip() for x in escape_info.split(seq_subitem_delimiter) if x.strip()]
    else:
        escapes = []

    if not escapes: return None

    return escapes

# parse sequence lines
def sequence_line_parser(line):
    line = line.rstrip(' ' + seq_item_delimiter + seq_subitem_delimiter + newline)
    line = line.lstrip(' ' + seq_subitem_delimiter + newline)
    # skip empty lines
    if not line: return None
    # do sequence line parsing
    seq_items = [x.strip() for x in line.split(seq_item_delimiter) if x.strip()]
    seq_item_count = len(seq_items)
    seq_cmd_args = [x for x in seq_items[0].split(' ') if x]
    cmd_keyword = seq_cmd_args[0]
    # initialize sequence command instance
    seq_cmd_inst = SequenceCommand()
    seq_cmd_inst['command'] = ' '.join(seq_cmd_args)
    seq_cmd_inst['argv'] = seq_cmd_args
    seq_cmd_inst['internal'] = False
    seq_cmd_inst['action'] = 'SEND'
    # lambda function for indexing list items
    g = lambda x, i: x[i] if i < len(x) else None
    # PARSE INTERNAL COMMANDS
    for k, v in COMMAND_ACTION_MAP.items():
        if re.search(k, cmd_keyword):
            seq_cmd_inst['internal'] = True
            seq_cmd_inst['action'] = v
            if seq_cmd_inst['action'] != 'ENTER':
                if seq_item_count > 1:
                    raise SequenceError('Invalid syntax for internal command: %s' %(line))
                if seq_cmd_inst['action'] == 'NEW_WORKER':
                    seq_cmd_inst['loops'] = int(seq_cmd_args[2]) if len(seq_cmd_args) > 2 else 1
                    prefix = sequence_file_entry[:sequence_file_entry.rfind(os.sep)+1]
                    path = seq_cmd_args[1]
                    seq_cmd_inst['sequence_file'] = path if os.sep in path else prefix + path
                    seq_cmd_inst['wait'] = True if 'WAIT' in cmd_keyword else False
                elif seq_cmd_inst['action'] == 'FIND':
                    seq_cmd_inst['target_file'] = seq_cmd_args[1]
                    seq_cmd_inst['find_dir'] = [x.strip(seq_subitem_delimiter) for x in seq_cmd_args[2:]]
    # check line item count
    if seq_item_count > 4:
        raise SequenceError('Invalid syntax for %s command: %s' \
                            %("INTERNAL" if seq_cmd_inst.internal else "SENDING", line))
    # PARSE CONNECTION COMMANDS
    if cmd_keyword in connect_commands:
        seq_cmd_inst['action'] = 'CONNECT'
        if cmd_keyword == 'ssh' and '@' in seq_cmd_args[1]:
            seq_cmd_inst['user'] = seq_cmd_args[1][:seq_cmd_args[1].index('@')]

        if seq_item_count > 1:
            try:
                seq_cmd_inst['timeout'] = float(seq_items[-1].strip())
                seq_items = seq_items[:-1]
            except ValueError:
                pass
        seq_cmd_inst['timeout'] = seq_cmd_inst['timeout']
        login_info = g(seq_items, 1)
        expect_info = g(seq_items, 2)
        escape_info = g(seq_items, 3)
        login_items = [x.strip() for x in login_info.split(seq_subitem_delimiter) if x.strip()] if login_info else []
        info1 = g(login_items, 0)
        info2 = g(login_items, 1)
        seq_cmd_inst['boot_expect'] = sequence_expect_parser(expect_info)
        seq_cmd_inst['boot_escape'] = sequence_escape_parser(escape_info)

        if seq_cmd_inst['user']:
            seq_cmd_inst['password'] = info2 if info2 else info1
        else:
            seq_cmd_inst['user'] = info1
            seq_cmd_inst['password'] = info2

        if cmd_keyword == 'ssh' and '@' not in seq_cmd_args[1] and seq_cmd_inst['user']:
            seq_cmd_inst['argv'][1] = seq_cmd_inst['user'] + '@' + seq_cmd_args[1]
            seq_cmd_inst['command'] = ' '.join(seq_cmd_inst['argv'])
    # PARSE NORMAL SENDING COMMANDS OR INTERNAL 'SEND-ENTER' COMMAND as they need to match prompt
    else:
        if seq_cmd_inst['action'] == 'SEND':
            seq_cmd_inst['bg_run'] = True if seq_cmd_inst['command'][-1] == '&' else False
        if not seq_cmd_inst['internal'] or seq_cmd_inst['action'] == 'ENTER':
            if seq_item_count > 1:
                try:
                    seq_cmd_inst['timeout'] = float(seq_items[-1].strip())
                    seq_items = seq_items[:-1]
                except ValueError:
                    pass
            seq_cmd_inst['timeout'] = seq_cmd_inst['timeout']
            expect_info = g(seq_items, 1)
            escape_info = g(seq_items, 2)
            seq_cmd_inst['expect'] = sequence_expect_parser(expect_info)
            seq_cmd_inst['escape'] = sequence_escape_parser(escape_info)
            # command needs to wait for passphrase, which means the prompt will be invisible.
            if seq_cmd_inst['expect'] is not None and \
                    len(seq_cmd_inst['expect']) == 1 and \
                    re.search(waitpassphrase_command_pattern, seq_cmd_inst['expect'][0], re.I):
                seq_cmd_inst['wait_passphrase'] = True

    return seq_cmd_inst


def sequence_finalize(test_seq):
    for index, item in enumerate(test_seq):
        # pass phrase is invisible in Pty terminal.
        if item['wait_passphrase']:
            test_seq[index+1]['text_invisible'] = True


def sequence_reader(sequence_file):
    if not os.path.exists(sequence_file):
        raise OSError('sequence file [%s] not found' %(sequence_file))

    test_seq = []
    with open(sequence_file, mode='r') as fp:
        preserved_line = ''
        for line in fp:
            line = utils._str(line)
            # skip sequence comments
            line = line[0:line.find(seq_comment_header)] if seq_comment_header in line else line
            line = line.strip()
            if not line: continue
            if line[-1] == seq_continue_nextline:
                preserved_line = preserved_line + line[0:-1].strip()
            else:
                if preserved_line:
                    line = preserved_line + line
                    preserved_line = ''
                inst = sequence_line_parser(line)
                if inst: test_seq.append(inst)

    sequence_finalize(test_seq)

    return test_seq
