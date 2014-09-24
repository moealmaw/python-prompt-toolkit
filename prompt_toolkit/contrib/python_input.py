"""

::

    from prompt_toolkit.contrib.python_import import PythonCommandLineInterface

    cli = PythonCommandLineInterface()
    cli.read_input()
"""
from __future__ import unicode_literals

from pygments.lexers import PythonLexer
from pygments.style import Style
from pygments.token import Keyword, Operator, Number, Name, Error, Comment, Token

from prompt_toolkit import CommandLineInterface
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.enums import InputMode
from prompt_toolkit.history import FileHistory, History
from prompt_toolkit.key_bindings.emacs import emacs_bindings
from prompt_toolkit.key_bindings.vi import vi_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.menus import CompletionMenu
from prompt_toolkit.layout.processors import BracketsMismatchProcessor
from prompt_toolkit.layout.prompt import Prompt
from prompt_toolkit.layout.toolbars import CompletionToolbar, ArgToolbar, SearchToolbar, ValidationToolbar
from prompt_toolkit.layout.toolbars import Toolbar
from prompt_toolkit.layout.utils import TokenList
from prompt_toolkit.line import Line
from prompt_toolkit.selection import SelectionType
from prompt_toolkit.validation import Validator, ValidationError
from prompt_toolkit.layout.margins import LeftMarginWithLineNumbers

import jedi
import platform
import re
import sys


__all__ = (
    'PythonCommandLineInterface',
    'AutoCompletionStyle',
)


_identifier_re = re.compile(r'[a-zA-Z_0-9_\.]+')


class AutoCompletionStyle:
    #: tab/double-tab completion
    # TRADITIONAL = 'traditional'  # TODO: not implemented yet.

    #: Pop-up
    POPUP_MENU = 'popup-menu'

    #: Horizontal list
    HORIZONTAL_MENU = 'horizontal-menu'

    #: No visualisation
    NONE = 'none'


class PythonStyle(Style):
    background_color = None
    styles = {
        Keyword:                       '#ee00ee',
        Operator:                      '#ff6666 bold',
        Number:                        '#ff0000',
        Name:                          '#008800',
        Name.Namespace:                '#008800 underline',
        Name.Decorator:                '#aa22ff',

        Token.Literal.String:          '#ba4444 bold',

        Error:                         '#000000 bg:#ff8888',
        Comment:                       '#0000dd',
        Token.Bash:                    '#333333',
        Token.IPython:                 '#660066',

        Token.IncrementalSearchMatch:         '#ffffff bg:#4444aa',
        Token.IncrementalSearchMatch.Current: '#ffffff bg:#44aa44',

        Token.SelectedText:            '#ffffff bg:#6666aa',

        # Signature highlighting.
        Token.Signature:               '#888888',
        Token.Signature.Operator:      'bold #888888',
        Token.Signature.CurrentName:   'bold underline #888888',

        # Highlighting for the reverse-search prompt.
        Token.Prompt:                     'bold #008800',
        Token.Prompt.ISearch:             'noinherit',
        Token.Prompt.ISearch.Text:        'bold',
        Token.Prompt.ISearch.Text.NoMatch: 'bg:#aa4444 #ffffff',

        Token.Prompt.SecondLinePrefix: 'bold #888888',
        Token.Prompt.LineNumber:       '#aa6666',
        Token.Prompt.Arg:              'noinherit',
        Token.Prompt.Arg.Text:          'bold',

        Token.Toolbar:                 'bg:#222222 #aaaaaa',
        Token.Toolbar.Off:             'bg:#222222 #888888',
        Token.Toolbar.On:              'bg:#222222 #ffffff',
        Token.Toolbar.Mode:            'bg:#222222 #ffffaa',
        Token.Toolbar.PythonVersion:   'bg:#222222 #ffffff bold',

        # Completion menu
        Token.CompletionMenu.Completion:             'bg:#888888 #ffffbb',
        Token.CompletionMenu.Completion.Current:     'bg:#dddddd #000000',
        Token.CompletionMenu.Meta.Current:           'bg:#bbbbbb #000000',
        Token.CompletionMenu.Meta:                   'bg:#888888 #cccccc',
        Token.CompletionMenu.ProgressBar:            'bg:#aaaaaa',
        Token.CompletionMenu.ProgressButton:         'bg:#000000',

        Token.CompletionToolbar.Completion:              '#888888 noinherit',
        Token.CompletionToolbar.Completion.Current:      'bold noinherit',
        Token.CompletionToolbar:                         'noinherit',
        Token.CompletionToolbar.Arrow:                   'bold #888888',

        # Grayed
        Token.Aborted:                 '#888888',

        Token.ValidationToolbar:         'bg:#440000 #aaaaaa',

        # Vi tildes
        Token.Leftmargin.Tilde:   '#888888',
    }


def _has_unclosed_brackets(text):
    """
    Starting at the end of the string. If we find an opening bracket
    for which we didn't had a closing one yet, return True.
    """
    stack = []

    # Ignore braces inside strings
    text = re.sub(r'''('[^']*'|"[^"]*")''', '', text)  # XXX: handle escaped quotes.!

    for c in reversed(text):
        if c in '])}':
            stack.append(c)

        elif c in '[({':
            if stack:
                if ((c == '[' and stack[-1] == ']') or
                        (c == '{' and stack[-1] == '}') or
                        (c == '(' and stack[-1] == ')')):
                    stack.pop()
            else:
                # Opening bracket for which we didn't had a closing one.
                return True

    return False


def python_bindings(registry, cli_ref):
    """
    Custom key bindings.
    """
    line = cli_ref().line
    handle = registry.add_binding

    @handle(Keys.F6)
    def _(event):
        """
        Enable/Disable paste mode.
        """
        line.paste_mode = not line.paste_mode
        if line.paste_mode:
            line.is_multiline = True

    if not cli_ref().line.always_multiline:
        @handle(Keys.F7)
        def _(event):
            """
            Enable/Disable multiline mode.
            """
            line.is_multiline = not line.is_multiline

    @handle(Keys.Tab, in_mode=InputMode.INSERT)
    @handle(Keys.Tab, in_mode=InputMode.COMPLETE)
    def _(event):
        """
        When the 'tab' key is pressed with only whitespace character before the
        cursor, do autocompletion. Otherwise, insert indentation.
        """
        current_char = line.document.current_line_before_cursor
        if not current_char or current_char.isspace():
            line.insert_text('    ')
        else:
            line.complete_next()
            if event.input_processor.input_mode != InputMode.COMPLETE:
                event.input_processor.push_input_mode(InputMode.COMPLETE)

    @handle(Keys.BackTab, in_mode=InputMode.INSERT)
    @handle(Keys.BackTab, in_mode=InputMode.COMPLETE)
    def _(event):
        """
        Shift+Tab: go to previous completion.
        """
        line.complete_previous()

        if event.input_processor.input_mode != InputMode.COMPLETE:
            event.input_processor.push_input_mode(InputMode.COMPLETE)
            line.complete_previous()


class PythonLine(Line):
    """
    Custom `Line` class with some helper functions.
    """
    def __init__(self, always_multiline, *a, **kw):
        self.always_multiline = always_multiline
        super(PythonLine, self).__init__(*a, **kw)

    def reset(self, *a, **kw):
        super(PythonLine, self).reset(*a, **kw)

        #: Boolean `paste` flag. If True, don't insert whitespace after a
        #: newline.
        self.paste_mode = False

        #: Boolean `multiline` flag. If True, [Enter] will always insert a
        #: newline, and it is required to use [Meta+Enter] execute commands.
        self.is_multiline = self.always_multiline

        # Code signatures. (This is set asynchronously after a timeout.)
        self.signatures = []

    def newline(self):
        r"""
        Insert \n at the cursor position. Also add necessary padding.
        """
        insert_text = super(PythonLine, self).insert_text

        if self.paste_mode or self.document.current_line_after_cursor:
            insert_text('\n')
        else:
            # Go to new line, but also add indentation.
            current_line = self.document.current_line_before_cursor.rstrip()
            insert_text('\n')

            # Copy whitespace from current line
            for c in current_line:
                if c.isspace():
                    insert_text(c)
                else:
                    break

            # If the last line ends with a colon, add four extra spaces.
            if current_line[-1:] == ':':
                for x in range(4):
                    insert_text(' ')

    @property
    def is_multiline(self):
        """
        Dynamically determine whether we're in multiline mode.
        """
        if self.always_multiline or self.paste_mode or '\n' in self.text:
            return True

        # If we just typed a colon, or still have open brackets, always insert a real newline.
        if (self.document.text_before_cursor.rstrip()[-1:] == ':' or
                                  _has_unclosed_brackets(self.document.text_before_cursor) or
                                  self.text.startswith('@')):
            return True

        # If the character before the cursor is a backslash (line continuation
        # char), insert a new line.
        elif (self.document.text_before_cursor[-1:] == '\\'):
            return True

        return False

    @is_multiline.setter
    def is_multiline(self, value):
        """ Ignore setter. """
        pass

    def complete_after_insert_text(self):
        """
        Start autocompletion when a we have a valid identifier before the
        cursor. (In this case it's not required to press [Tab] in order to view
        the completion menu.)
        """
        word_before_cursor = self.document.get_word_before_cursor()
        return word_before_cursor is not None and _identifier_re.match(word_before_cursor)


class SignatureToolbar(Toolbar):
    def is_visible(self, cli):
        return super(SignatureToolbar, self).is_visible(cli) and bool(cli.line.signatures)

    def get_tokens(self, cli, width):
        result = []
        append = result.append
        Signature = Token.Signature

        if cli.line.signatures:
            sig = cli.line.signatures[0]  # Always take the first one.

            append((Token, '           '))
            append((Signature, sig.full_name))
            append((Signature.Operator, '('))

            for i, p in enumerate(sig.params):
                if i == sig.index:
                    append((Signature.CurrentName, str(p.name)))
                else:
                    append((Signature, str(p.name)))
                append((Signature.Operator, ', '))

            result.pop()  # Pop last comma
            append((Signature.Operator, ')'))
        return result


class PythonToolbar(Toolbar):
    def __init__(self, vi_mode):
        self.vi_mode = vi_mode
        super(PythonToolbar, self).__init__()

    def get_tokens(self, cli, width):
        TB = Token.Toolbar # XXX: use self.token
        mode = cli.input_processor.input_mode

        result = TokenList()
        append = result.append

        append((TB, ' '))

        # Mode
        if mode == InputMode.INCREMENTAL_SEARCH:
            append((TB.Mode, '(SEARCH)'))
            append((TB, '   '))
        elif self.vi_mode:
            if mode == InputMode.INSERT:
                append((TB.Mode, '(INSERT)'))
                append((TB, '   '))
            elif mode == InputMode.VI_SEARCH:
                append((TB.Mode, '(SEARCH)'))
                append((TB, '   '))
            elif mode == InputMode.VI_NAVIGATION:
                append((TB.Mode, '(NAV)'))
                append((TB, '      '))
            elif mode == InputMode.VI_REPLACE:
                append((TB.Mode, '(REPLACE)'))
                append((TB, '  '))
            elif mode == InputMode.COMPLETE:
                append((TB.Mode, '(COMPLETE)'))
                append((TB, ' '))
            elif mode == InputMode.SELECTION and self.line.selection_state:
                if self.line.selection_state.type == SelectionType.LINES:
                    append((TB.Mode, '(VISUAL LINE)'))
                    append((TB, ' '))
                elif self.line.selection_state.type == SelectionType.CHARACTERS:
                    append((TB.Mode, '(VISUAL)'))
                    append((TB, ' '))

        else:
            append((TB.Mode, '(emacs)'))
            append((TB, ' '))

        # Position in history.
        append((TB, '%i/%i ' % (cli.line.working_index + 1, len(cli.line._working_lines))))

        # Shortcuts.
        if mode == InputMode.INCREMENTAL_SEARCH:
            append((TB, '[Ctrl-G] Cancel search [Enter] Go to this position.'))
        elif mode == InputMode.SELECTION and not cli.vi_mode:
            # Emacs cut/copy keys.
            append((TB, '[Ctrl-W] Cut [Meta-W] Copy [Ctrl-Y] Paste [Ctrl-G] Cancel'))
        else:
            if cli.line.paste_mode:
                append((TB.On, '[F6] Paste mode (on)  '))
            else:
                append((TB.Off, '[F6] Paste mode (off) '))

            if not cli.always_multiline:
                if cli.line.is_multiline:
                    append((TB.On, '[F7] Multiline (on)'))
                else:
                    append((TB.Off, '[F7] Multiline (off)'))

            if cli.line.is_multiline:
                append((TB, ' [Meta+Enter] Execute'))

            # Python version
            version = sys.version_info
            append((TB, ' - '))
            append((TB.PythonVersion, '%s %i.%i.%i' % (platform.python_implementation(),
                   version.major, version.minor, version.micro)))

        # Adjust toolbar width.
        if len(result) > width:
            # Trim toolbar
            result = result[:width - 3]
            result.append((TB, ' > '))
        else:
            # Extend toolbar until the page width.
            result.append((TB, ' ' * (width - len(result))))

        return result


'''
class PythonPrompt(Prompt):
    @property
    def completion_menu(self):
        style = self.cli.autocompletion_style

        if style == AutoCompletionStyle.POPUP_MENU:
            return PopupCompletionMenu()
        elif style == AutoCompletionStyle.HORIZONTAL_MENU:
            return None

        elif self.cli.autocompletion_style == AutoCompletionStyle.HORIZONTAL_MENU and \
                self.line.complete_state and \
                self.cli.input_processor.input_mode == InputMode.COMPLETE:
            HorizontalCompletionMenu().write(screen, None, self.line.complete_state)

'''

class PythonLeftMargin(LeftMarginWithLineNumbers):
    def width(self, cli):
        return len('In [%s]: ' % cli.current_statement_index)

    def write(self, cli, screen, y, line_number):
        if y == 0:
            screen.write_highlighted([
                (Token.Prompt, 'In [%s]: ' % cli.current_statement_index)
            ])


class PythonValidator(Validator):
    def validate(self, document):
        """
        Check input for Python syntax errors.
        """
        try:
            compile(document.text, '<input>', 'exec')
        except SyntaxError as e:
            # Note, the 'or 1' for offset is required because Python 2.7
            # gives `None` as offset in case of '4=4' as input. (Looks like
            # fixed in Python 3.)
            raise ValidationError(e.lineno - 1, (e.offset or 1) - 1, 'Syntax Error')
        except TypeError as e:
            # e.g. "compile() expected string without null bytes"
            raise ValidationError(0, 0, str(e))


def get_jedi_script_from_document(document, locals, globals):
    try:
        return jedi.Interpreter(
            document.text,
            column=document.cursor_position_col,
            line=document.cursor_position_row + 1,
            path='input-text',
            namespaces=[locals, globals])

    except jedi.common.MultiLevelStopIteration:
        # This happens when the document is just a backslash.
        return None
    except ValueError:
        # Invalid cursor position.
        # ValueError('`column` parameter is not in a valid range.')
        return None


class PythonCompleter(Completer):
    def __init__(self, globals, locals):
        super(PythonCompleter, self).__init__()

        self._globals = globals
        self._locals = locals


    def get_completions(self, document):
        """ Ask jedi to complete. """
        script = get_jedi_script_from_document(document, self._locals, self._globals)

        if script:
            for c in script.completions():
                yield Completion(c.name_with_symbols, len(c.complete) - len(c.name_with_symbols),
                                 display=c.name_with_symbols)


class PythonCommandLineInterface(CommandLineInterface):
    def __init__(self,
                 globals=None, locals=None,
                 stdin=None, stdout=None,
                 vi_mode=False, history_filename=None,
                 style=PythonStyle, autocompletion_style=AutoCompletionStyle.POPUP_MENU,
                 always_multiline=False):

        self.globals = globals or {}
        self.locals = locals or globals
        self.always_multiline = always_multiline
        self.autocompletion_style = autocompletion_style

        layout = Layout(
                input_processors = [BracketsMismatchProcessor()],
                min_height=7,
                lexer = PythonLexer,
                left_margin = PythonLeftMargin(),
                menus=[CompletionMenu()],
                bottom_toolbars =[
                        ArgToolbar(),
                        SignatureToolbar(),
                        SearchToolbar(),
                        ValidationToolbar(),
                        CompletionToolbar(),
                        PythonToolbar(vi_mode=vi_mode),
                    ],
                show_tildes=True)

        if history_filename:
            history = FileHistory(history_filename)
        else:
            history = History()

        if vi_mode:
           key_binding_factories = [vi_bindings, python_bindings]
        else:
           key_binding_factories = [emacs_bindings, python_bindings]

        #: Incremeting integer counting the current statement.
        self.current_statement_index = 1

        self.get_signatures_thread_running = False

        super(PythonCommandLineInterface, self).__init__(
            layout=layout,
            style=style,
            key_binding_factories=key_binding_factories,
            line=PythonLine(always_multiline=always_multiline,
                            tempfile_suffix='.py',
                            history=history,
                            completer=PythonCompleter(self.globals, self.locals),
                            validator=PythonValidator()))

    def on_input_timeout(self):
        """
        When there is no input activity,
        in another thread, get the signature of the current code.
        """
        # Never run multiple get-signature threads.
        if self.get_signatures_thread_running:
            return
        self.get_signatures_thread_running = True

        document = self.line.document

        def run():
            script = get_jedi_script_from_document(document, self.locals, self.globals)

            # Show signatures in help text.
            if script:
                try:
                    signatures = script.call_signatures()
                except ValueError:
                    # e.g. in case of an invalid \\x escape.
                    signatures = []
                except Exception:
                    # Sometimes we still get an exception (TypeError), because
                    # of probably bugs in jedi. We can silence them.
                    signatures = []
            else:
                signatures = []

            self.get_signatures_thread_running = False

            # Set signatures and redraw if the text didn't change in the
            # meantime. Otherwise request new signatures.
            if self.line.text == document.text:
                self.line.signatures = signatures
                self.request_redraw()
            else:
                self.on_input_timeout()

        self.run_in_executor(run)
