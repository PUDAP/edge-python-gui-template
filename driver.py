"""
Example driver class for a machine.

Methods marked with ``@command`` are advertised to PUDA and callable through the
CLI or a protocol. Undecorated methods are neither advertised nor callable, so
helpers and one-off utilities stay internal. Document every command with a
docstring, arguments, and return type. A command signals failure by raising;
returning False still counts as success.

Add ``@safety(...)`` under ``@command`` to publish hazards and preconditions
with the command. It is advisory for agents and operators and does not block
dispatch; enforce real preconditions in the method body.

Methods marked with ``@tlm_stream(interval=...)`` are polled every ``interval``
seconds in a separate worker thread and published to
``puda.<machine_id>.tlm.stream.<name>``. They can run concurrently with
commands, which also run in worker threads.

The ``@machine_state`` method's dict is merged into every MACHINE_STATE update
(e.g. homed flag, loaded labware). At most one is allowed.

EdgeRunner publishes the heartbeat and host health (CPU, memory, temperature)
on its own.
"""

from puda import command, machine_state, safety, tlm_stream


class Driver:
    """TODO: One-sentence summary of this machine, shown by puda machine list and info."""

    def __init__(self, port: str):
        # TODO: Open the connection to the machine (e.g. serial.Serial(port, 9600))
        self._port = port
        self._homed = False
        self._position = None

    @machine_state # only one machine_state method is allowed
    def snapshot(self) -> dict:
        """
        Extra fields merged into MACHINE_STATE updates.

        Called on each state change (startup, command start and end, errors,
        shutdown), not on a timer. Runs on the event loop, so return cached
        values only; do not read from the device here. Use @tlm_stream for
        values that change outside commands.
        """
        return {"homed": self._homed, "position": self._position}

    @command
    def shutdown(self) -> bool:
        """
        Shutdown the machine. Releases all resources and connections to the machine.

        Returns:
            bool: True if the shutdown was successful, False otherwise
        """
        return True

    @command
    def home(self) -> bool:
        """
        Homes the machine. Used by PUDA CLI `puda machine home <machine_id>`

        Returns:
            bool: True if the home was successful, False otherwise
        """
        self._homed = True
        return True

    @command
    def reset(self) -> bool:
        """
        Software reset the machine. Used by PUDA CLI `puda machine reset <machine_id>`

        Returns:
            bool: True if the reset was successful, False otherwise
        """
        self._homed = False
        return True

    @command
    @safety(  # advisory context shown to agents; does not block the command
        summary="Collision risk from moving an unhomed axis or into an occupied workspace.",
        hazards=["collision", "pinch"],
        requires="Machine must be homed.",
        forbidden_when="Do not move while a person is reaching into the workspace.",
        confirm=True,  # agents must ask the operator for confirmation before running it
    )
    def move_to(self, x: float, y: float, z: float) -> dict:
        """
        Move to an absolute position.

        Args:
            x: Target X in mm
            y: Target Y in mm
            z: Target Z in mm

        Returns:
            dict: The position after the move
        """
        if not self._homed:
            raise RuntimeError("Machine is not homed; run home first")
        # TODO: Send the move to the machine and wait until it finishes
        self._position = {"x": x, "y": y, "z": z}
        return self._position

    @tlm_stream(interval=3.0, name="pos") # stream every 3 seconds to puda.<machine_id>.tlm.stream.pos
    def get_position(self) -> dict:
        """
        Current position of the machine. Remove if the machine has no position sensor.

        As a @tlm_stream, this is called every 3 seconds in a separate worker
        thread and may run at the same time as a command. Return None to skip
        a sample; exceptions are logged and the stream keeps running. The
        reading is cached in self._position so snapshot can include it.

        Returns:
            dict: A dictionary containing the current position of the machine
        """
        # TODO: Read the position from the machine
        self._position = {"x": 0, "y": 0, "z": 0}
        return self._position
