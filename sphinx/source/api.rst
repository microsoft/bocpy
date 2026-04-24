.. _api:

API
===

.. module:: bocpy

This part of the documentation covers all the interfaces of `bocpy`.

Behaviors
---------

.. autoclass:: Cown
    :members:
    :undoc-members:

.. autofunction:: wait
.. autodecorator:: when
.. autofunction:: start


Noticeboard
-----------

.. autofunction:: notice_write
.. autofunction:: notice_update
.. autofunction:: notice_delete
.. autofunction:: noticeboard
.. autofunction:: notice_read
.. autodata:: REMOVED


Math
----

.. autoclass:: Matrix
    :members:
    :undoc-members:
    :special-members: __init__


Messaging
---------

.. autofunction:: send
.. autofunction:: receive
.. autofunction:: set_tags
.. autofunction:: drain
