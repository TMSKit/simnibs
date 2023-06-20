.. _localite_doc:

Localite
========

This module provides import and export functions for the `Localite <http://localite.de>`_ TMS Navigator software.

The Localite ecosystem provides two main options to store coil positions/orientations:


InstrumentMarkers
------------------
These are often set manually by the user during an experimental session. They show up in the TMS Navigator GUI under `InstrumentMarkers`, can individually be named, and are visualized by a red cross in the 3D view of the TMS Navigator.

:code:`InstrumentMarkers` can be used if one wants to post-hoc compute one single electrical field for an experimental condition (-> :ref:`import`). On the other hand, :code:`InstrumentMarkers` can be generated for a specific coil position with SimNIBS, for example after running an E-field optimization (-> :ref:`export`).

Storage location in Localite TMS Navigator patients folder:

.. code-block:: bash

    subject_folder/Sessions/Session_%datetime%/InstrumentMarkers/InstrumentMarker%datetime%.xml

Example :code:`InstrumentMarker` .xml file:

.. code-block:: xml

    <?xml version="1.0" encoding="UTF-8"?>
    <InstrumentMarkerList coordinateSpace="RAS">
        <InstrumentMarker alwaysVisible="false" index="0" selected="false">
            <Marker additionalInformation="" color="#ff0000" description="M1" set="true">
                <Matrix4D data00="1.0" data01="0.0" data02="0.0" data03="0.0"
                          data10="0.0" data11="1.0" data12="0.0" data13="0.0"
                          data20="0.0" data21="0.0" data22="1.0" data23="0.0"
                          data30="0.0" data31="0.0" data32="0.0" data33="1.0"/>
            </Marker>
        </InstrumentMarker>
    <!-- more <InstrumentMarker> objects here -->
    </InstrumentMarkerList>

TriggerMarkers
--------------
These are saved automatically after hitting the `Record Stimulation Markers` button. For each pulse sent from the stimulator, positions/orientations for coil 1 and coil 2 are stored. These are visualized as arrows in the 3D view of the TMS Navigator. :code:`TriggerMarkers` can be used to post-hoc compute all realized stimulations during an experiment.

:code:`TriggerMarkers` can also store the realized stimulator (%MSO) and stimulation (dIdt) intensity.

Storage location in Localite TMS Navigator patients folder:

.. code-block:: bash

    subject_folder/Sessions/Session_%datetime%/TMSTrigger/TriggerMarkers_CoilX_$datetime$.xml

.. code-block:: xml
   :caption: Example :code:`TriggerMarkers` .xml file

    <?xml version="1.0" encoding="UTF-8"?>
    <TriggerMarkerList coordinateSpace="RAS" isOnlineReading="false" startTime="13:55:07.658">
        <ResponseParameters selectedResponseChannel="0">
            <responseDescription id="response" maxValue="51200.0"
                minValue="-51200.0" name="Response" unit="uV"/>
            <responseDescription id="valueA" maxValue="200.0" minValue="0.0"
                name="Value A (di/dt)" unit="A/us"/>
            <responseDescription id="amplitudeA" maxValue="100.0"
                minValue="0.0" name="Amplitude A" unit="%"/>
        </ResponseParameters>
        <TriggerMarker color="#ffafaf" description="" recordingTime="52358"
            selected="false" set="false" visibility="true">
            <ResponseValues>
                <Value key="valueA" response="61.0"/>
                <Value key="response" response="NaN"/>
                <Value key="amplitudeA" response="44.0"/>
            </ResponseValues>
            <Matrix4D data00="1.0" data01="0.0" data02="0.0" data03="0.0"
                data10="0.0" data11="1.0" data12="0.0" data13="0.0"
                data20="0.0" data21="0.0" data22="1.0" data23="0.0"
                data30="0.0" data31="0.0" data32="0.0" data33="1.0"/>
        </TriggerMarker>
        <!-- (Many) more <TriggerMarker> objects here -->
    </TriggerMarkerList>

How to use
-----------

.. _import:

Import to SimNIBS
#################

:code:`simnibs.localite.read(fn)` reads :code:`InstrumentMarker` and :code:`TriggerMarker` .xml files and returns a :code:`simnibs.TMSLIST()` object. The conversion from TMS Navigator coordinate system (i.e. coil axes definition and enforced 'RAS') to SimNIBS coordinate system is performed automatically.

..  code-block:: python
    :caption: Import a single :code:`TriggerMarker` .xml file as a :code:`simnibs.TMSLIST()`

    from simnibs import sim_struct, localite

    s = sim_struct.SESSION()

    fn = "subject_folder/Sessions/Session_%datetime%/TMSTrigger/TriggerMarkers_CoilX_$datetime$.xml"
    tms_list = localite().read(fn)  # read all TriggerMarkers from file and return as TMSLIST()
    s.add_tmslist(tms_list)

    tms_list.pos[0].didt  # <- stimulation intensity is filled with data from .xml if available or defaults to 1 A/µs.
    tms_list.pos[0].name  # <- name is filled with data from .xml if available or defaults to ''.

.. _export:

Export from SimNIBS
###################

:code:`simnibs.localite.write(obj, fn)` writes an .xml file that is compatible with TMS Navigator :code:`InstrumentMarker` .xml files. The conversion from SimNIBS TMS Navigator coil axes definition is performed automatically.

**Caution**: The world coordinate system of the T1 scan used in the TMS Navigator has to be set correctly. Rule of thumb: Nifti -> 'RAS' and DICOM -> 'LPS'.

.. code-block:: python
    :caption: Export a :code:`TriggerMarker` .xml file for precomputed positions/orientations

    from simnibs import sim_struct, opt_struct, localite
    fn = "precomuted_InstrumentMarker.xml"

    ### export from TMSLIST
    tmslist = sim_struct.TMSLIST()
    tmslist.add_position()
    # ... define (multiple) positions ...
    localite().write(tmlist, fn, out_coord_space='LPS')

    ### export from POSITION
    pos = sim_struct.POSITOIN()
    pos.matsimnibs = ...
    localite().write(pos, fn) # out_coord_space default is 'RAS'

    ### export from np.ndarray / matsimnibs
    opt = opt_struct.TMSoptimize()
    # ... prepare optmization ...
    opt_mat = opt.run() # get optimal position
    localite().write(opt_mat, fn)

The generated .xml file can now to be imported into the TMS Navigator Session structure. This can be done manually by copy-pasting the contents of the .xml file into the correct (~ last) :code:`InstrumentMarker` .xml file. Alternatively, one can use the (not official) `IMporter <https://gitlab.gwdg.de/tms-localization/utils/importer>`_ tool.

Notes
------
* The **same anatomical scan** has to be used for TMS Navigator and SimNIBS.
* The **same coil model** has to be used for field simulations and for real stimulation.
* The **correct world coordinate system** ('RAS' or 'LPS') used in TMS Navigator has to be set when writing :code:`InstrumentMarker` .xml files. SimNIBS always uses 'RAS'.
* TMS Navigator stores one :code:`TriggerMarker` file per coil, even if only one coil is calibrated:

  * Coil 1 -> TriggerMarkers_Coil0_$datetime$.xml
  * Coil 2 -> TriggerMarkers_Coil1_$datetime$.xml
* When a coil is not tracked during a pulse, the :code:`TriggerMarker` position/orientation matrix is filled with :code:`0`. These are automatically removed by :code:`simnibs.localite.write(obj, fn)`.
* TMS Navigator saves many :code:`TriggerMarker` and :code:`InstrumentMarker` .xml files during an experimental session. The very last ones are good candidates to contain all data for the experimental session.
* Coordinate systems used to define coil axes for SimNIBS and Localite:

.. figure:: ../../images/coil_axesorientation_localite.png

Links
-----
Importer tool: https://gitlab.gwdg.de/tms-localization/utils/importer

Localite TMS Navigator: https://www.localite.de/en/products/tms-navigator/

\

