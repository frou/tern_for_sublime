import sublime, sublime_plugin
from Default.history_list import get_jump_history_for_view

import os, sys, platform, subprocess, webbrowser, json, re, time, atexit
import tempfile, textwrap
import urllib.request, urllib.error

# http://ternjs.net/doc/manual.html

# The Emacs version is likely more comprehensible: https://github.com/ternjs/tern/tree/master/emacs

# TODO: Make the completions async to improve usability. See https://github.com/nsf/gocode/pull/531#issuecomment-445950433 and any similar work I do on GoFeather

tern_command = ["tern", "--no-port-file"]

files = {}
documentation_panel_name = "tern_documentation"

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

windows = platform.system() == "Windows"
localhost = (windows and "127.0.0.1") or "localhost"


class Listeners(sublime_plugin.ViewEventListener):
    @classmethod
    def is_applicable(cls, settings):
        return settings_indicate_js(settings)

    def on_close(self):
        view = self.view
        files.pop(view.file_name(), None)

    def on_deactivated_async(self):
        view = self.view
        pfile = files.get(view.file_name(), None)
        if pfile and pfile.dirty:
            send_buffer(pfile, view)

    def on_modified(self):
        view = self.view
        pfile = files.get(view.file_name(), None)
        if pfile:
            pfile_modified(pfile, view)

    def on_query_completions(self, prefix, locations):
        view = self.view
        sel = sel_start(view.sel()[0])

        pfile = get_pfile(view)
        if pfile is None:
            return None

        completions, fresh = ensure_completions_cached(pfile, view)
        if completions is None:
            return None

        if not fresh:
            completions = [c for c in completions if c[1].startswith(prefix)]

        return (completions, sublime.INHIBIT_WORD_COMPLETIONS)


class ProjectFile(object):
    def __init__(self, name, view, project):
        self.project = project
        self.name = name
        self.dirty = view.is_dirty()
        self.cached_completions = None
        self.cached_arguments = None
        self.last_modified = 0


class Project(object):
    def __init__(self, dir):
        self.dir = dir
        self.port = None
        self.proc = None
        self.last_failed = 0

    def __del__(self):
        kill_server(self)


def settings_indicate_js(view_settings):
    return (
        view_settings.get("syntax")
        == "Packages/JavaScript/JavaScript.sublime-syntax"
    )


def get_pfile(view):
    if not settings_indicate_js(view.settings()):
        return None
    fname = view.file_name()
    if fname is None:
        fname = os.path.join(tempfile.gettempdir(), "tfs_%s" % time.time())
    if fname in files:
        return files[fname]

    pdir = project_dir(fname)
    if pdir is None:
        return None

    project = None
    for f in files.values():
        if f.project.dir == pdir:
            project = f.project
            break
    pfile = files[fname] = ProjectFile(fname, view, project or Project(pdir))
    return pfile


def project_dir(fname):
    dir = os.path.dirname(fname)
    if not os.path.isdir(dir):
        return None

    cur = dir
    while True:
        parent = os.path.dirname(cur[:-1])
        if not parent:
            break
        if os.path.isfile(os.path.join(cur, ".tern-project")):
            return cur
        cur = parent
    return dir


def pfile_modified(pfile, view):
    pfile.dirty = True
    now = time.time()
    if now - pfile.last_modified > 0.5:
        pfile.last_modified = now
        sublime.set_timeout_async(
            lambda: maybe_save_pfile(pfile, view, now), 5000
        )
    if (
        pfile.cached_completions
        and sel_start(view.sel()[0]) < pfile.cached_completions[0]
    ):
        pfile.cached_completions = None
    if (
        pfile.cached_arguments
        and sel_start(view.sel()[0]) < pfile.cached_arguments[0]
    ):
        pfile.cached_arguments = None


def maybe_save_pfile(pfile, view, timestamp):
    if pfile.last_modified == timestamp and pfile.dirty:
        send_buffer(pfile, view)


def server_port(project, ignored=None):
    if project.port is not None and project.port != ignored:
        return (project.port, True)
    if project.port == ignored:
        kill_server(project)

    port_file = os.path.join(project.dir, ".tern-port")
    if os.path.isfile(port_file):
        port = int(open(port_file, "r").read())
        if port != ignored:
            project.port = port
            return (port, True)

    started = start_server(project)
    if started is not None:
        project.port = started
    return (started, False)


def start_server(project):
    if time.time() - project.last_failed < 30:
        return None
    env = None
    if platform.system() == "Darwin":
        env = os.environ.copy()
        env["PATH"] += ":/usr/local/bin"

    proc = subprocess.Popen(
        tern_command,
        cwd=project.dir,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=windows,
    )
    output = ""

    while True:
        line = proc.stdout.readline().decode("utf-8")
        if not line:
            sublime.error_message(
                "Failed to start server" + (output and ":\n" + output)
            )
            project.last_failed = time.time()
            return None
        match = re.match("Listening on port (\\d+)", line)
        if match:
            project.proc = proc
            return int(match.group(1))
        else:
            output += line


def kill_server(project):
    if project.proc is None:
        return
    project.proc.stdin.close()
    project.proc.wait()
    project.proc = None


def relative_file(pfile):
    return pfile.name[len(pfile.project.dir) + 1 :]


def count_indentation(line):
    count, pos = (0, 0)
    while pos < len(line):
        ch = line[pos]
        if ch == " ":
            count += 1
        elif ch == "\t":
            count += 4
        else:
            break
        pos += 1
    return count


def sel_start(sel):
    return min(sel.a, sel.b)


def sel_end(sel):
    return max(sel.a, sel.b)


def make_request(port, doc):
    req = opener.open(
        "http://" + localhost + ":" + str(port) + "/",
        json.dumps(doc).encode("utf-8"),
        1,
    )
    return json.loads(req.read().decode("utf-8"))


def view_full_text(view):
    return view.substr(sublime.Region(0, view.size()))


def run_command(view, query, pos=None):
    """Run the query on the Tern server.

  See default queries at http://ternjs.net/doc/manual.html#protocol.
  """

    pfile = get_pfile(view)
    if pfile is None:
        return

    if isinstance(query, str):
        query = {"type": query}
    if pos is None:
        pos = view.sel()[0].b

    port, port_is_old = server_port(pfile.project)
    if port is None:
        return

    doc = {"query": query, "files": []}

    if not pfile.dirty:
        fname, sending_file = (relative_file(pfile), False)

    doc["files"].append(
        {
            "type": "full",
            "name": relative_file(pfile),
            "text": view_full_text(view),
        }
    )
    fname, sending_file = ("#0", True)
    query["file"] = fname
    query["end"] = pos

    data = None
    try:
        data = make_request(port, doc)
        if data is None:
            return None
    except:
        pass

    if data is None and port_is_old:
        try:
            port = server_port(pfile.project, port)[0]
            if port is None:
                return
            data = make_request(port, doc)
            if data is None:
                return None
        except Exception as e:
            sublime.status_message("TERN ERROR: " + str(e))

    if sending_file:
        pfile.dirty = False
    return data


def send_buffer(pfile, view):
    port = server_port(pfile.project)[0]
    if port is None:
        return False
    try:
        make_request(
            port,
            {
                "files": [
                    {
                        "type": "full",
                        "name": relative_file(pfile),
                        "text": view_full_text(view),
                    }
                ]
            },
        )
        pfile.dirty = False
        return True
    except:
        return False


def completion_icon(type):
    # print(type)
    if type is None:
        return ""
    if type == "?":
        return type
    if type.startswith("fn("):
        return "Function"
    if type.startswith("["):
        return "Array"
    if type == "number":
        return "Number"
    if type == "string":
        return "String"
    if type == "bool":
        return "Boolean"
    return "Object"


# create function argument list (including parenthesis) with internal snippets
# to tab between argument placeholders.
def create_arg_str(arguments):
    if len(arguments) == 0:
        # Have the () itself be a snippet, to normalise the number of keyboard
        # interactions to deal with the argument list. i.e. no matter the number of
        # arguments, at least one tab press is required to advance beyond it.
        return "${1:()}"

    arg_str = ""
    snippet_idx = 1

    for argument in arguments:
        # $ is a valid character in JS identifiers, but needs escaped when used in
        # a Sublime snippet.
        argument = argument.replace("$", "\\$")

        pre_sep = ""
        if snippet_idx > 1:
            pre_sep = ", "

        if argument.endswith("?"):  # optional argument?
            snippet_idx_inner = snippet_idx + 1
            arg_str += "${%d:%s${%d:%s}}" % (
                snippet_idx,
                pre_sep,
                snippet_idx_inner,
                argument,
            )
            snippet_idx += 2
        else:
            arg_str += "%s${%d:%s}" % (pre_sep, snippet_idx, argument)
            snippet_idx += 1

    return "(" + arg_str + ")"


# parse the type to get the arguments
def get_arguments(type):
    type = type[3 : type.find(")")] + ",'"
    arg_list = []
    arg_start = 0
    arg_end = 0
    # this two variables are used to skip ': {...}' in signature like 'a: {...}'
    depth = 0
    arg_already = False
    for ch in type:
        if depth == 0 and ch == ",":
            if arg_already:
                arg_already = False
            elif arg_start != arg_end:
                arg_list.append(type[arg_start:arg_end])
            arg_start = arg_end + 1
        elif depth == 0 and ch == ":":
            arg_already = True
            arg_list.append(type[arg_start:arg_end])
        elif ch == "{" or ch == "(" or ch == "[":
            depth += 1
        elif ch == "}" or ch == ")" or ch == "]":
            depth -= 1
        elif ch == " ":
            arg_start = arg_end + 1
        arg_end += 1
    return arg_list


def ensure_completions_cached(pfile, view):
    pos = view.sel()[0].b
    if pfile.cached_completions is not None:
        c_start, c_word, c_completions = pfile.cached_completions
        if c_start <= pos:
            slice = view.substr(sublime.Region(c_start, pos))
            if slice.startswith(c_word) and not re.match(".*\\W", slice):
                return (c_completions, False)

    data = run_command(
        view, {"type": "completions", "types": True, "filter": False}
    )
    # print(data)
    if data is None:
        return (None, False)

    completions = []
    completions_arity = []
    for rec in data["completions"]:
        # print(rec)
        rec_name = rec.get("name")
        # To Sublime, dollars are related to snippet placeholders.
        rec_name_escaped = rec_name.replace("$", "\\$")
        rec_type = rec.get("type", None)
        if rec_type is not None and rec_type.startswith("fn("):
            arguments = get_arguments(rec_type)

            fn_name = rec_name + " ("
            if len(arguments) > 0:
                fn_name += "…"
            fn_name += ")"

            category = "func"
            hint = category.ljust(7) + " " + fn_name
            typ = completion_icon(parse_function_type(rec).get("retval"))
            if typ != "":
                hint += "\t" + typ

            replacement = rec_name_escaped

            placeholder_snippets = create_arg_str(arguments)
            replacement += placeholder_snippets

            completions.append((hint, replacement))

        else:
            category = "var"
            hint = category.ljust(7) + " " + rec_name
            typ = completion_icon(rec_type)
            if typ != "":
                hint += "\t" + typ

            replacement = rec_name_escaped

            completions.append((hint, replacement))

    # put the auto completions of functions with lower arity at the bottom of the autocomplete list
    # so they don't clog up the autocompeltions at the top of the list
    completions = completions + completions_arity
    pfile.cached_completions = (
        data["start"],
        view.substr(sublime.Region(data["start"], pos)),
        completions,
    )
    return (completions, True)


def locate_call(view):
    # Select the current identifier
    view.run_command("move", {"by": "wordends", "forward": True})
    view.run_command("move", {"by": "words", "forward": False, "extend": True})
    selection = view.sel()[0]

    # Record the position that Tern considers a 'call':
    #
    # foo(
    # ...^
    retval = (selection.b, 0)  # arg index 0 (doesn't matter for panel use)

    # Leave the cursor at the start of the identifier, with no selection:
    #
    # foo(
    # ^
    view.run_command("move", {"by": "characters", "forward": False})

    return retval


def prepare_documentation(pfile, view):
    doc_url = None

    call_start, argpos = locate_call(view)
    if call_start is None:
        return (render_documentation(pfile, view, None, 0), doc_url)

    if (
        pfile.cached_arguments is not None
        and pfile.cached_arguments[0] == call_start
    ):
        parsed = pfile.cached_arguments[1]
        doc_url = parsed["url"]
        render_documentation(pfile, view, parsed, argpos)
        return (True, doc_url)

    data = run_command(
        view, {"type": "type", "preferFunction": True}, call_start
    )
    if data is not None:
        parsed = parse_function_type(data)
        doc_url = data.get("url", None)

        if parsed is not None:
            parsed["url"] = doc_url
            parsed["doc"] = data.get("doc", None)
            pfile.cached_arguments = (call_start, parsed)
            render_documentation(pfile, view, parsed, argpos)
            return (True, doc_url)

    sublime.status_message("TERN: CAN'T FIND DOCUMENTATION")
    return (False, doc_url)


def get_documentation_panel(window):
    return window.get_output_panel(documentation_panel_name)


def get_message_from_ftype(ftype, argpos):
    msg = ftype["name"] + "("
    i = 0
    for name, type in ftype["args"]:
        if i > 0:
            msg += ", "
        if i == argpos:
            msg += "*"
        msg += name + ("" if type == "?" else ": " + type)
        i += 1
    msg += ")"
    if ftype["retval"] is not None:
        msg += " -> " + ftype["retval"]
    if ftype["doc"] is not None:
        msg += "\n\n" + textwrap.fill(ftype["doc"], width=79)
    return msg


def render_documentation(pfile, view, ftype, argpos):
    panel = get_documentation_panel(view.window())

    if ftype is None:
        panel.run_command("tern_insert_documentation", {"msg": ""})
    else:
        panel.run_command(
            "tern_insert_documentation",
            {"msg": get_message_from_ftype(ftype, argpos)},
        )


def parse_function_type(data):
    type = data["type"]
    if not re.match("fn\\(", type):
        return None
    pos = 3
    args, retval = ([], None)
    while pos < len(type) and type[pos] != ")":
        colon = type.find(":", pos)
        name = "?"
        if colon != -1:
            name = type[pos:colon]
            if not re.match("[\\w_$]+$", name):
                name = "?"
            else:
                pos = colon + 2
        type_start = pos
        depth = 0
        while pos < len(type):
            ch = type[pos]
            if ch == "(" or ch == "[" or ch == "{":
                depth += 1
            elif ch == ")" or ch == "]" or ch == "}":
                if depth > 0:
                    depth -= 1
                else:
                    break
            elif ch == "," and depth == 0:
                break
            pos += 1
        args.append((name, type[type_start:pos]))
        if type[pos] == ",":
            pos += 2
    if type[pos : pos + 5] == ") -> ":
        retval = type[pos + 5 :]
    return {
        "name": data.get("exprName", None) or data.get("name", None) or "fn",
        "args": args,
        "retval": retval,
    }


def encode_current_position(view):
    row, col = view.rowcol(view.sel()[0].begin())
    return view.file_name() + ":" + str(row + 1) + ":" + str(col + 1)


class TernInsertDocumentation(sublime_plugin.TextCommand):
    def run(self, edit, **args):
        self.view.insert(edit, 0, args.get("msg", ""))


class TernShowDocumentation(sublime_plugin.TextCommand):
    def run(self, args):
        view = self.view
        window = view.window()

        pfile = get_pfile(view)
        if not pfile:
            return

        documentation_panel_full_name = "output.%s" % documentation_panel_name

        (ok, doc_url) = prepare_documentation(pfile, view)
        if ok:
            window.run_command(
                "show_panel", {"panel": documentation_panel_full_name}
            )
        else:
            msg = "Could not find documentation text"
            if doc_url is None:
                sublime.status_message("TERN: " + msg.upper())
            else:
                msg += ", but documentation is available on the web. Open it in browser?"
                if sublime.ok_cancel_dialog(msg):
                    webbrowser.open(doc_url)


class TernJumpToDef(sublime_plugin.TextCommand):
    def run(self, edit, **args):
        data = run_command(
            self.view, {"type": "definition", "lineCharPositions": True}
        )
        if data is None:
            return

        file = data.get("file", None)
        if not file:
            sublime.status_message("TERN: COULD NOT FIND DEFINITION")
            return

        get_jump_history_for_view(self.view).push_selection(self.view)

        real_file = (
            os.path.join(get_pfile(self.view).project.dir, file)
            + ":"
            + str(data["start"]["line"] + 1)
            + ":"
            + str(data["start"]["ch"] + 1)
        )
        sublime.active_window().open_file(real_file, sublime.ENCODED_POSITION)


class TernShowType(sublime_plugin.TextCommand):
    def run(self, edit, **args):
        data = run_command(self.view, {"type": "documentation"})
        if data is None:
            return
        sublime.message_dialog(data.get("type"))
