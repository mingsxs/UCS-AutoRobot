import sys


CURSOR_CONTROL = "\033["
CURSOR_UPWARDS = 'A'
CURSOR_DOWNWARDS = 'B'
CURSOR_FORWARDS = 'C'
CURSOR_BACKWARDS = 'D'

ERASE_LINE = CURSOR_CONTROL + "2K"
CLEAR_SCREEN = CURSOR_CONTROL + "2J"

# move up to lines
def move_up_lines(line=1):
    ctrl_char = CURSOR_CONTROL + str(line) + CURSOR_UPWARDS
    sys.stdout.write(ctrl_char)

# move down to lines
def move_down_lines(line=1):
    ctrl_char = CURSOR_CONTROL + str(line) + CURSOR_DOWNWARDS
    sys.stdout.write(ctrl_char)

# move right forward columns
def move_forward_columns(column=1):
    ctrl_char = CURSOR_CONTROL + str(column) + CURSOR_FORWARDS
    sys.stdout.write(ctrl_char)

# move left backward columns
def move_backward_columns(column=1):
    ctrl_char = CURSOR_CONTROL + str(column) + CURSOR_BACKWARDS
    sys.stdout.write(ctrl_char)

# clear screen
def clear_screen():
    sys.stdout.write(CLEAR_SCREEN)

# erase line
def erase_line():
    sys.stdout.write(ERASE_LINE)

def erase_lines_upward(line=1):
    cursor_lines = line
    while cursor_lines > 0:
        erase_line()
        move_up_lines()
        cursor_lines -= 1

def erase_lines_downward(line=1):
    cursor_lines = line
    while cursor_lines > 0:
        erase_line()
        move_down_lines()
        cursor_lines -= 1
