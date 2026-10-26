import inspect
import logging
import os
import re
import sys

# Cache that stores context name computed from file paths to make translations faster
FILEPATH_TO_CONTEXT = {}

# Cache that stores the source file path found by inspect for each code file name (`co_filename`) that tr() is called from.
# inspect.getsourcefile() is slow (it may scan all loaded modules), therefore it is only called once for each code file.
_CODE_FILENAME_TO_SOURCE_FILE = {}

# Placeholders (replacement fields) in a format string: all text delimited by curly braces,
# except the ones enclosed in double-braces.
# See https://docs.python.org/3/library/string.html#formatstrings
_PLACEHOLDER_PATTERN = re.compile(r"(?<!\{)\{([^\{\}]+)\}(?!\})")

def translate(context, text):
    """Translate message to the current application language.
    This `translate(context, message)` function is recognized by Qt's lupdate tool.
    See example use in DICOM module.
    """
    try:
        from slicer import app
        return app.translate(context, text)
    except ImportError:
        return text


def getContext(sourceFile):
    """Get the translation context name.

    The context name is constructed from the Python package name (name of the parent folder that contains `__init__.py` file - if it exists)
    and the source file name (without the `.py` extension).

    Most Slicer modules do not have a `__init__.py` file in their folder, so the context name is simply the source file name
    (for example, `DICOMEnhancedUSVolumePlugin`).

    Most helper Python scripts in Slicer are Python packages (subfolders containing addition Python scripts and an `__init__.py` file)
    and their name is constructed as PythonPackageName.SourceFileName (for example, `SegmentEditorEffects.SegmentEditorDrawEffect`).
    """

    # Cache context name to make translations faster
    if sourceFile in FILEPATH_TO_CONTEXT:
        return FILEPATH_TO_CONTEXT[sourceFile]

    if os.path.isfile(sourceFile):
        parentFolder = os.path.dirname(sourceFile)
        init_file_path = parentFolder + os.path.sep + "__init__.py"

        if os.path.isfile(init_file_path):
            context_name = os.path.basename(parentFolder)
            context_name += "." + os.path.basename(sourceFile).replace(".py", "")
        else:
            context_name = os.path.basename(sourceFile).replace(".py", "")
    else:
        context_name = os.path.basename(sourceFile)

    FILEPATH_TO_CONTEXT[sourceFile] = context_name
    return context_name


def tr(text):
    """Translation function for python scripted modules that automatically determines context name.

    This is more convenient to use than `translate(context, text)` because the developer does not need to manually specify the context.
    This function is typically imported as `_` function.

    Example::

      from slicer.i18n import tr as _
      ...
      statusText = _("Idle") if idle else _("Running")

    """

    # Get the source file of the caller from the cache to make translations faster.
    # inspect.stack() would get all the frames, which can take more than 100ms, therefore we use lower level APIs.
    frame = sys._getframe(1)  # go up in the stack one level
    filename = _CODE_FILENAME_TO_SOURCE_FILE.get(frame.f_code.co_filename)
    if filename is None:
        filename = inspect.getsourcefile(frame) or inspect.getfile(frame)
        _CODE_FILENAME_TO_SOURCE_FILE[frame.f_code.co_filename] = filename

    contextName = getContext(filename)
    translatedText = translate(contextName, text)

    # Accept the translation only if all placeholders are present in the translated text to prevent runtime errors.
    # For example:
    #   text = "delete {count} files"
    #   translatedText = "supprimer {compter} fichiers" (incorrect, because `count` should not have been translated)
    # would fail at runtime with a KeyError when `_("delete {count} files").format(count=numberOfSomeItems)` is called,
    # as after translation it turns into `"supprimer {compter} fichiers".format(count=numberOfSomeItems)`.
    # The check prevents the runtime error: only a warning is logged and the incorrect translation is ignored.
    # The check is skipped if the text is not translated (for example, the application language is English).
    if translatedText != text and set(_PLACEHOLDER_PATTERN.findall(text)) != set(_PLACEHOLDER_PATTERN.findall(translatedText)):
        logging.warning(f"In context '{contextName}', translation of '{text}' to '{translatedText}' is incorrect: placeholders do not match")
        return text

    return translatedText
