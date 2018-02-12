# encoding=utf8

import abc
import cgi
import textwrap

import sublime

def format_doc(doc):
  """Format doc output for display in panel."""

  return textwrap.fill(doc, width=79)

def get_message_from_ftype(ftype, argpos):
  msg = ftype["name"] + "("
  i = 0
  for name, type in ftype["args"]:
    if i > 0: msg += ", "
    if i == argpos: msg += "*"
    msg += name + ("" if type == "?" else ": " + type)
    i += 1
  msg += ")"
  if ftype["retval"] is not None:
    msg += " -> " + ftype["retval"]
  if ftype['doc'] is not None:
    msg += "\n\n" + format_doc(ftype['doc'])
  return msg

class RendererBase(object):
  """Class that renders Tern messages."""

  __metaclass__ = abc.ABCMeta

  @abc.abstractmethod
  def _render_impl(self, pfile, view, message):
    """Render the message.

    Implement this to define how subclasses render the message.
    """

  def _clean_impl(self, pfile, view):
    """Clean rendered content.

    Override this to define subclass-specific cleanup.
    """
    pass

  def _render_message(self, pfile, view, message):
    self._render_impl(pfile, view, message)
    pfile.showing_arguments = True

  def render_arghints(self, pfile, view, ftype, argpos):
    """Render argument hints."""

    if self.useHTML:
      message = get_html_message_from_ftype(ftype, argpos)
    else:
      message = get_message_from_ftype(ftype, argpos)
    self._render_message(pfile, view, message)

  def clean(self, pfile, view):
    """Clean rendered content."""

    self._clean_impl(pfile, view)
    pfile.showing_arguments = False

class PanelRenderer(RendererBase):
  """Class that renders Tern messages in a panel."""

  def __init__(self):
    self.useHTML = False

  def _render_impl(self, pfile, view, message):
    panel = view.window().get_output_panel("tern_arghint")
    panel.run_command("tern_arghint", {"msg": message})
    # view.window().run_command("show_panel", {"panel": "output.tern_arghint"})

  def _clean_impl(self, pfile, view):
    if pfile.showing_arguments:
      panel = view.window().get_output_panel("tern_arghint")
      panel.run_command("tern_arghint", {"msg": ""})
