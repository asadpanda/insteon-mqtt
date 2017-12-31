#===========================================================================
#
# Command sequence class
#
#===========================================================================
from . import log
from . import util

LOG = log.get_logger()


class CommandSeq:
    """Series of commands to run sequentially.

    This class stores a series of commands that run sequentially (using
    on_done callbacks to trigger the next command).  If any command fails, it
    stops the sequence.

    Ideally there is a better way to this but I couldn't come up with any.
    Since each call is via callback, the stack grows longer and longer so
    this shouldn't be used for 100+ calls but for the small number of calls
    this library needs, it works ok.

    This type of class is needed because we need to send series of commands.
    And each one needs to return so that the event loop can process the
    network activity to actually run the command.  In the future, this is
    probably a good case for switching to asyncio type processing.
    """
    #-----------------------------------------------------------------------
    def __init__(self, protocol, msg=None, on_done=None):
        """Constructor

        Args:
          protocol: The Protocol object to use.
          msg:      (str) String message to pass to on_done if the
                    sequence works.
          on_done:  The callback to run when complete.  This will be run
                    when there is an error or when all the commands finish.
        """
        self.protocol = protocol

        self._on_done = util.make_callback(on_done)
        self.msg = msg
        self.total = 0

        # List of Entry objects (see class below) to call for each step in
        # the sequence.
        self.calls = []

    #-----------------------------------------------------------------------
    def add(self, func, *args, **kwargs):
        """Add a function call to the sequence.

        This will call the input function with the supplied arguments when
        it's next in the sequence.

        Args:
          func:     The function or method to call.  Must take an on_done
                    callback argument.
          args:     Arguments to pass to the function.
          kwargs:   Keyword arguments to pass to the function.

        """
        # Sequence override on_done calls to any function but some calls need
        # to set it anyway because of kwarg name ordering requirements.  So
        # remote it here to avoid getting a duplicate keyword error later..
        if "on_done" in kwargs:
            del kwargs["on_done"]
        self.calls.append(Entry.from_func(func, args, kwargs))
        self.total += 1

    #-----------------------------------------------------------------------
    def add_msg(self, msg, handler):
        """Add a message and handler to the sequence.

        This will pass the message and handler to the protocol object when
        it's next in the sequence.

        NOTE: the on_done callback in the handler will NOT be called.  The
        on_done callback supplied to the CommandSeq constructor will be
        called if this is the last entry.

        Args:
          msg:      The message object to send.
          handler:  The handler to use for the message.
        """
        self.calls.append(Entry.from_msg(msg, handler))
        self.total += 1

    #-----------------------------------------------------------------------
    def run(self):
        """Run the sequence.

        Depending on the functions in the sequence, this generally returns
        right away.  When the current command finishes, the on_done callback
        to that command triggers the next call.
        """
        self.on_done(True, None, None)

    #-----------------------------------------------------------------------
    def on_done(self, success, msg, data):
        """TODO: doc
        """
        # Last function failed with an error.
        if not success:
            self._on_done(success, msg, data)

        # No more calls - success.
        elif not self.calls:
            self._on_done(success, self.msg, data)

        # Otherwise run the next command.
        else:
            LOG.debug("Running command %d of %d", self.total + 1 -
                      len(self.calls), self.total)

            entry = self.calls.pop(0)
            entry.run(self.protocol, self.on_done)

    #-----------------------------------------------------------------------


#===========================================================================
class Entry:
    #pylint: disable=attribute-defined-outside-init

    @classmethod
    def from_func(cls, func, args, kwargs):
        obj = cls()
        obj.msg = None
        obj.func = func
        obj.args = args
        obj.kwargs = kwargs
        return obj

    #-----------------------------------------------------------------------
    @classmethod
    def from_msg(cls, msg, handler):
        obj = cls()
        obj.func = None
        obj.msg = msg
        obj.handler = handler
        return obj

    #-----------------------------------------------------------------------
    def run(self, protocol, on_done):
        if self.func is None:
            self.handler.on_done = on_done
            protocol.send(self.msg, self.handler)

        else:
            self.func(*self.args, on_done=on_done, **self.kwargs)

#===========================================================================
