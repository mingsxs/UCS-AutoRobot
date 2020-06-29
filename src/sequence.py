import os
from os import linesep as newline
import re

from agent import (connect_commands,
                   waitpassphrase_command_pattern)
from const import (seq_comment_header,
                   seq_continue_nextline,
                   seq_item_delimiter,
                   seq_subitem_delimiter,
                   sequence_file_entry,
                   builtin_monitor_interval)
from builtin import (BuiltinCommand,
                     match_builtin_command)

import utils
from utils import SequenceError


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
    seq_cmd_inst = BuiltinCommand()
    seq_cmd_inst['command'] = ' '.join(seq_cmd_args)
    seq_cmd_inst['argv'] = seq_cmd_args
    seq_cmd_inst['builtin'] = False
    seq_cmd_inst['action'] = 'SEND'
    # lambda function for indexing list items
    g = lambda x, i: x[i] if i < len(x) else None

    # PARSE BUILTIN COMMANDS
    match = match_builtin_command(cmd_keyword)
    if match:
        seq_cmd_inst['builtin'] = True
        seq_cmd_inst['action'] = match
        if seq_cmd_inst['action'] != 'ENTER':
            # parse 'NEW WORKER' arguments
            if seq_cmd_inst['action'] == 'NEW_WORKER':
                if seq_item_count > 1:
                    raise SequenceError('Invalid syntax for builtin command %s: %s' %(cmd_keyword, line))
                seq_cmd_inst['loops'] = int(seq_cmd_args[2]) if len(seq_cmd_args) > 2 else 1
                prefix = sequence_file_entry[:sequence_file_entry.rfind(os.sep)+1]
                path = seq_cmd_args[1]
                seq_cmd_inst['sequence_file'] = path if os.sep in path else prefix + path
                seq_cmd_inst['wait'] = True if 'WAIT' in cmd_keyword else False
            # parse 'FIND' arguments
            elif seq_cmd_inst['action'] == 'FIND':
                if seq_item_count > 1:
                    raise SequenceError('Invalid syntax for builtin command %s: %s' %(cmd_keyword, line))
                seq_cmd_inst['target_file'] = seq_cmd_args[1]
                seq_cmd_inst['find_dir'] = [x.strip(' ') for x in ''.join(seq_cmd_args[2:]).split(seq_subitem_delimiter)]
            # parse 'MONITOR' arguments
            elif seq_cmd_inst['action'] == 'MONITOR':
                if seq_item_count > 3:
                    raise SequenceError('Invalid syntax for builtin command %s: %s' %(cmd_keyword, line))
                try:
                    seq_cmd_inst['interval'] = float(seq_items[-1].strip())
                except ValueError:
                    seq_cmd_inst['interval'] = builtin_monitor_interval
                seq_cmd_inst['command'] = ' '.join(seq_cmd_args[1:])
                watch = sequence_escape_parser(g(seq_items, 1))
                seq_cmd_inst['watch'] = watch if watch else []

    # check line item count
    if seq_item_count > 4:
        raise SequenceError('Invalid syntax for %s command: %s' \
                            %("BUILTIN" if seq_cmd_inst.builtin else "SENDING", line))

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

    # PARSE NORMAL SENDING COMMANDS OR BUILTIN 'SEND-ENTER' COMMAND as they need to match prompt
    else:
        if not seq_cmd_inst['builtin'] or seq_cmd_inst['action'] == 'ENTER':
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

    return seq_cmd_inst


def sequence_finalize(test_seq):
    for index, item in enumerate(test_seq):
        # commands which will run in background mode
        if item['action'] == 'SEND':
            test_seq[index]['bg_run'] = True if item['command'][-1] == '&' else False
        # command needs to wait for passphrase, which means the prompt will be invisible.
        if item['expect'] is not None and \
                len(item['expect']) == 1 and \
                re.search(waitpassphrase_command_pattern, item['expect'][0], re.I):
            test_seq[index]['command_wait_passphrase'] = True
            # pass phrase is invisible in Pty terminal.
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
