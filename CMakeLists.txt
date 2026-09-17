# mu2edaq-file-reaper is a pure Python application; this CMake file exists so
# the mu2edaq-main build harness (and anyone used to `cmake --build`) can
# create the venv, run the tests and install the man pages uniformly on
# Linux, macOS and Windows.
#
#   cmake -S . -B build && cmake --build build            # bootstrap venv
#   cmake --build build --target test                      # pytest
#   cmake --install build --prefix /usr/local              # man pages + scripts
cmake_minimum_required(VERSION 3.16)
project(mu2edaq-file-reaper VERSION 0.1.0 LANGUAGES NONE)

find_package(Python3 3.9 REQUIRED COMPONENTS Interpreter)

set(VENV_DIR "${CMAKE_SOURCE_DIR}/venv")
if(WIN32)
  set(VENV_PY "${VENV_DIR}/Scripts/python.exe")
  set(BOOTSTRAP powershell -ExecutionPolicy Bypass -File "${CMAKE_SOURCE_DIR}/bootstrap.ps1")
else()
  set(VENV_PY "${VENV_DIR}/bin/python")
  set(BOOTSTRAP "${CMAKE_SOURCE_DIR}/bootstrap.sh")
endif()

add_custom_command(
  OUTPUT "${VENV_PY}"
  COMMAND ${CMAKE_COMMAND} -E env MU2EDAQ_PYTHON=${Python3_EXECUTABLE} ${BOOTSTRAP}
  WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
  COMMENT "Bootstrapping the mu2edaq-file-reaper virtual environment")
add_custom_target(venv ALL DEPENDS "${VENV_PY}")

add_custom_target(pytest
  COMMAND "${VENV_PY}" -m pytest -q
  WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
  DEPENDS venv
  COMMENT "Running the pytest suite")

enable_testing()
add_test(NAME pytest COMMAND "${VENV_PY}" -m pytest -q WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}")

install(DIRECTORY man/ DESTINATION share/man FILES_MATCHING PATTERN "*.[1357]")
install(PROGRAMS start-mu2edaq-file-reaper.sh stop-mu2edaq-file-reaper.sh bootstrap.sh
        DESTINATION share/mu2edaq-file-reaper)
install(DIRECTORY config/ DESTINATION share/mu2edaq-file-reaper/config FILES_MATCHING PATTERN "*.yaml")
