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


def sequence_check_builtin_syntax(word, line, limit, count):
    if count > limit:
        raise SequenceError('Invalid syntax for BUILTIN command %s: %s' %(word, line))

def sequence_check_normal_syntax(word, line, limit, count):
    if count > limit:
        raise SequenceError('Invalid syntax for SENDING command %s: %s' %(word, line))

# parse expect info
def sequence_expect_parser(expect_info):
    if expect_info:
        #expects = [x.strip() for x in expect_info.split(seq_subitem_delimiter) if x.strip()]
        expects = utils.sequence_item_split(expect_info, seq_subitem_delimiter)
    else:
        expects = []

    if not expects: return None

    if len(expects) == 1 and 'PROMPT' in expects: return None

    return expects

# parse escape info
def sequence_escape_parser(escape_info):
    if escape_info:
        #escapes = [x.strip() for x in escape_info.split(seq_subitem_delimiter) if x.strip()]
        escapes = utils.sequence_item_split(escape_info, seq_subitem_delimiter)
    else:
        escapes = []

    if not escapes: return None

    return escapes

# parse sequence lines
def sequence_line_parser(line):
    # skip empty lines
    if not line: return None
    # do sequence line parsing
    #seq_items = [x.strip() for x in line.split(seq_item_delimiter) if x.strip()]
    seq_items = utils.sequence_item_split(line, seq_item_delimiter)
    seq_item_count = len(seq_items)
    if not seq_item_count: return None
    #seq_cmd_args = [x for x in seq_items[0].split(' ') if x]
    seq_cmd_args = utils.sequence_item_split(seq_items[0], ' ')
    cmd_keyword = seq_cmd_args[0] if seq_cmd_args else 'SEND-ENTER'
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
                sequence_check_builtin_syntax(cmd_keyword, line, 1, seq_item_count)
                sequence_check_builtin_syntax(cmd_keyword, line, 3, len(seq_cmd_args))
                seq_cmd_inst['loops'] = int(seq_cmd_args[2]) if len(seq_cmd_args) > 2 else 1
                prefix = sequence_file_entry[:sequence_file_entry.rfind(os.sep)+1]
                path = seq_cmd_args[1]
                seq_cmd_inst['sequence_file'] = path if os.sep in path else prefix + path
                seq_cmd_inst['wait'] = True if 'WAIT' in cmd_keyword else False
            # parse 'FIND' arguments
            elif seq_cmd_inst['action'] == 'FIND':
                sequence_check_builtin_syntax(cmd_keyword, line, 1, seq_item_count)
                seq_cmd_inst['target_file'] = seq_cmd_args[1]
                seq_cmd_inst['find_dir'] = [x.strip(' ') for x in utils.sequence_item_split(''.join(seq_cmd_args[2:]), seq_subitem_delimiter)]
            # parse 'MONITOR' arguments
            elif seq_cmd_inst['action'] == 'MONITOR':
                sequence_check_builtin_syntax(cmd_keyword, line, 3, seq_item_count)
                try:
                    seq_cmd_inst['interval'] = float(seq_items[-1].strip())
                except ValueError:
                    seq_cmd_inst['interval'] = builtin_monitor_interval
                seq_cmd_inst['command'] = ' '.join(seq_cmd_args[1:])
                watch = sequence_escape_parser(g(seq_items, 1))
                seq_cmd_inst['watch'] = watch if watch else []

    # check line item count
    if seq_cmd_inst.builtin:
        sequence_check_builtin_syntax(seq_cmd_inst.command, line, 4, seq_item_count)
    else:
        sequence_check_normal_syntax(seq_cmd_inst.command, line, 4, seq_item_count)

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
        login_items = utils.sequence_item_split(login_info, seq_subitem_delimiter) if login_info else []
        info1 = g(login_items, 0)
        info2 = g(login_items, 1)
        seq_cmd_inst['boot_expect'] = sequence_expect_parser(expect_info)
        seq_cmd_inst['boot_escape'] = sequence_escape_parser(escape_info)

        if seq_cmd_inst['user']:
            seq_cmd_inst['password'] = info2 if info2 else info1
        else:
            seq_cmd_inst['user'] = info1.strip() if info1 else info1
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
            seq_comment_header_pos = line.find(seq_comment_header)
            if seq_comment_header_pos >= 0:
                line = line[0 : seq_comment_header_pos]
            # strip ' \n' in right side for all lines, put ';' to the end of line for expect_info
            line = line.rstrip()

            if not line: continue

            if line[-1] == seq_continue_nextline:
                preserved_line = preserved_line + line[0:-1]
            else:
                if preserved_line:
                    line = preserved_line + line
                    preserved_line = ''
                inst = sequence_line_parser(line)
                if inst: test_seq.append(inst)

    sequence_finalize(test_seq)

    return test_seq
