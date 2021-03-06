#  _________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2014 Sandia Corporation.
#  Under the terms of Contract DE-AC04-94AL85000 with Sandia Corporation,
#  the U.S. Government retains certain rights in this software.
#  This software is distributed under the BSD License.
#  _________________________________________________________________________

import itertools
import logging
import os
import subprocess
import re
import tempfile

import pyutilib.services
from pyutilib.misc import Options

import pyomo.util.plugin as plugin
from pyomo.opt.base import *
from pyomo.opt.base.solvers import _extract_version
from pyomo.opt.results import *
from pyomo.opt.solver import *
from pyomo.core.base import SortComponents
from pyomo.core.base.objective import Objective
from pyomo.core.base import Constraint
from pyomo.core.base.set_types import *
from pyomo.repn.plugins.baron_writer import ProblemWriter_bar

from six.moves import xrange, zip

logger = logging.getLogger('pyomo.solvers')

class BARONSHELL(SystemCallSolver):
    """The BARON MINLP solver
    """

    plugin.alias('baron',  doc='The BARON MINLP solver')

    def __init__(self, **kwds):
        #
        # Call base class constructor
        #
        kwds['type'] = 'baron'
        SystemCallSolver.__init__(self, **kwds)

        self._tim_file = None

        self._valid_problem_formats=[ProblemFormat.bar]
        self._valid_result_formats = {}
        self._valid_result_formats[ProblemFormat.bar] = [ResultsFormat.soln]
        self.set_problem_format(ProblemFormat.bar)

        self._capabilities = Options()
        self._capabilities.linear = True
        self._capabilities.quadratic_objective = True
        self._capabilities.quadratic_constraint = True
        self._capabilities.integer = True
        self._capabilities.sos1 = False
        self._capabilities.sos2 = False


        # CLH: Coppied from cpxlp.py, the cplex file writer.
        # Keven Hunter made a nice point about using %.16g in his attachment
        # to ticket #4319. I am adjusting this to %.17g as this mocks the
        # behavior of using %r (i.e., float('%r'%<number>) == <number>) with
        # the added benefit of outputting (+/-). The only case where this
        # fails to mock the behavior of %r is for large (long) integers (L),
        # which is a rare case to run into and is probably indicative of
        # other issues with the model.
        # *** NOTE ***: If you use 'r' or 's' here, it will break code that
        #               relies on using '%+' before the formatting character
        #               and you will need to go add extra logic to output
        #               the number's sign.
        self._precision_string = '.17g'

    @staticmethod
    def license_is_valid(executable='baron'):
        """
        Runs a check for a valid Baron license using the
        given executable (default is 'baron'). All output is
        hidden. If the test fails for any reason (including
        the executable being invalid), then this function
        will return False.
        """
        with tempfile.NamedTemporaryFile(mode='w',
                                         delete=False) as f:
            with tempfile.NamedTemporaryFile(mode='w',
                                             delete=False) as fr:
                pass
            f.write("//This is a dummy .bar file created to "
                    "return the baron version//\n"
                    "OPTIONS {\n"
                    "ResName: \""+fr.name+"\";\n"
                    "Summary: 0;\n"
                    "}\n"
                    "POSITIVE_VARIABLES x1;\n"
                    "OBJ: minimize x1;")
        try:
            rc = subprocess.call([executable, f.name],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        except OSError:
            rc = 1
        finally:
            try:
                os.remove(fr.name)
            except OSError:
                pass
            try:
                os.remove(f.name)
            except OSError:
                pass
        if rc:
            return False
        else:
            return True

    def _default_executable(self):
        executable = pyutilib.services.registered_executable("baron")
        if executable is None:
            logger.warning("Could not locate the 'baron' executable, "
                           "which is required for solver %s" % self.name)
            self.enable = False
            return None
        return executable.get_path()

    def _get_version(self):
        """
        Returns a tuple describing the solver executable version.
        """
        solver_exec = self.executable()

        if solver_exec is None:
            return _extract_version('')
        else:
            dummy_prob_file = tempfile.NamedTemporaryFile(mode='w')
            dummy_prob_file.write("//This is a dummy .bar file created to "
                                  "return the baron version//\nPOSITIVE_VARIABLES "
                                  "x1;\nOBJ: minimize x1;")
            dummy_prob_file.seek(0)
            results = pyutilib.subprocess.run( [solver_exec,dummy_prob_file.name])
            return _extract_version(results[1],length=3)

    def create_command_line(self, executable, problem_files):

        # The solution file is created in the _convert_problem function.
        # The bar file needs the solution filename in the OPTIONS section, but
        # this function is executed after the bar problem file writing.
        #self._soln_file = pyutilib.services.TempfileManager.create_tempfile(suffix = '.baron.sol')


        cmd = [executable, problem_files[0]]
        if self._timer:
            cmd.insert(0, self._timer)
        return pyutilib.misc.Bunch( cmd=cmd,
                                    log_file=self._log_file,
                                    env=None )

    #
    # Assuming the variable values stored in the model will
    # automatically be included in the Baron input file
    # (returning True implies the opposite and requires another function)
    def warm_start_capable(self):

        return False

    def _convert_problem(self,
                         args,
                         problem_format,
                         valid_problem_formats,
                         **kwds):

        # Baron needs all solver options and file redirections
        # inside the input file, so we need to input those
        # here through io_options before calling the baron writer

        #
        # Define log file
        #
        if self._log_file is None:
            self._log_file = pyutilib.services.TempfileManager.\
                            create_tempfile(suffix = '.baron.log')

        #
        # Define solution file
        #
        if self._soln_file is None:
            self._soln_file = pyutilib.services.TempfileManager.\
                              create_tempfile(suffix = '.baron.soln')

        self._tim_file = pyutilib.services.TempfileManager.\
                         create_tempfile(suffix = '.baron.tim')

        #
        # Create options to send through as io_options
        # containing all relevent info needed in the Baron file
        #
        solver_options = {}
        solver_options['ResName'] = self._soln_file
        solver_options['TimName'] = self._tim_file
        for key in self.options:
            lower_key = key.lower()
            if lower_key == 'resname':
                logger.warning('The ResName option is set to %s'
                               % self._soln_file)
            elif lower_key == 'timname':
                logger.warning('The TimName option is set to %s'
                               % self._tim_file)
            else:
                solver_options[key] = self.options[key]

        if 'solver_options' in kwds:
            raise ValueError("Baron solver options should be set "
                             "using the options object on this "
                             "solver plugin. The solver_options "
                             "I/O options dict for the Baron writer "
                             "will be populated by this plugin's "
                             "options object")
        kwds['solver_options'] = solver_options

        return OptSolver._convert_problem(self,
                                          args,
                                          problem_format,
                                          valid_problem_formats,
                                          **kwds)

    def process_logfile(self):

        results = SolverResults()

        #
        # Process logfile
        #
        OUTPUT = open(self._log_file)

        # Collect cut-generation statistics from the log file
        for line in OUTPUT:
            if 'Bilinear' in line:
                results.solver.statistics['Bilinear_cuts'] = int(line.split()[1])
            elif 'LD-Envelopes' in line:
                results.solver.statistics['LD-Envelopes_cuts'] = int(line.split()[1])
            elif 'Multilinears' in line:
                results.solver.statistics['Multilinears_cuts'] = int(line.split()[1])
            elif 'Convexity' in line:
                results.solver.statistics['Convexity_cuts'] = int(line.split()[1])
            elif 'Integrality' in line:
                results.solver.statistics['Integrality_cuts'] = int(line.split()[1])

        OUTPUT.close()
        return results


    def process_soln_file(self, results):
        # check for existence of the solution and time file. Not sure why we
        # just return - would think that we would want to indicate
        # some sort of error
        if not os.path.exists(self._soln_file):
            logger.warning("Solution file does not exist: %s" % (self._soln_file))
            return
        if not os.path.exists(self._tim_file):
            logger.warning("Time file does not exist: %s" % (self._tim_file))
            return

        with open(self._tim_file, "r") as TimFile:
            with open(self._soln_file,"r") as INPUT:
                self._process_soln_file(results, TimFile, INPUT)

    def _process_soln_file(self, results, TimFile, INPUT):
        #
        # **NOTE: This solution parser assumes the baron input file
        #         was generated by the Pyomo baron_writer plugin, and
        #         that a dummy constraint named c_e_FIX_ONE_VAR_CONST__
        #         was added as the initial constraint in order to
        #         support trivial constraint equations arrising from
        #         fixing pyomo variables. Thus, the dual price solution
        #         information for the first constraint in the solution
        #         file will be excluded from the results object.
        #

        # TODO: Is there a way to hanle non-zero return values from baron?
        #       Example: the "NonLinearity Error if POW expression"
        #       (caused by  x ^ y) when both x and y are variables
        #       causes an ugly python error and the solver log has a single
        #       line to display the error, hard to pick out of the list

        # Check for suffixes to send back to pyomo
        extract_marginals = False
        extract_price = False
        for suffix in self._suffixes:
            flag = False
            if re.match(suffix, "rc"): #baron_marginal
                extract_marginals = True
                flag = True
            if re.match(suffix, "dual"): #baron_price
                extract_price = True
                flag = True
            if not flag:
                raise RuntimeError("***The BARON solver plugin cannot"
                                   "extract solution suffix="+suffix)

        soln = Solution()

        #
        # Process model and solver status from the Baron tim file
        #
        line = TimFile.readline().split()
        results.problem.name = line[0]
        results.problem.number_of_constraints = int(line[1])
        results.problem.number_of_variables = int(line[2])
        results.problem.lower_bound = float(line[5])
        results.problem.upper_bound = float(line[6])
        soln.gap = results.problem.upper_bound - results.problem.lower_bound
        solver_status = line[7]
        model_status = line[8]

        objective = None
        ##try:
        ##    objective = symbol_map.getObject("__default_objective__")
        ##    objective_label = symbol_map_byObjects[id(objective)]
        ##except:
        ##    objective_label = "__default_objective__"
        # [JDS 17/Feb/15] I am not sure why this is needed, but all
        # other solvers (in particular the ASL solver and CPLEX) always
        # return the objective value in the __default_objective__ label,
        # and not by the Pyomo object name.  For consistency, we will
        # do the same here.
        objective_label = "__default_objective__"

        soln.objective[objective_label] = {'Value': None}
        results.problem.number_of_objectives = 1
        if objective is not None:
            results.problem.sense = \
                'minimizing' if objective.is_minimizing() else 'maximizing'

        if solver_status == '1':
            results.solver.status = SolverStatus.ok
        elif solver_status == '2':
            results.solver.status = SolverStatus.error
            results.solver.termination_condition = TerminationCondition.error
            #CLH: I wasn't sure if this was double reporting errors. I
            #     just filled in one termination_message for now
            results.solver.termination_message = \
                ("Insufficient memory to store the number of nodes required "
                 "for this seach tree. Increase physical memory or change "
                 "algorithmic options")
        elif solver_status == '3':
            results.solver.status = SolverStatus.ok
            results.solver.termination_condition = \
                TerminationCondition.maxIterations
        elif solver_status == '4':
            results.solver.status = SolverStatus.ok
            results.solver.termination_condition = \
                TerminationCondition.maxTimeLimit
        elif solver_status == '5':
            results.solver.status = SolverStatus.warning
            results.solver.termination_condition = \
                TerminationCondition.other
        elif solver_status == '6':
            results.solver.status = SolverStatus.aborted
            results.solver.termination_condition = \
                TerminationCondition.userInterrupt
        elif solver_status == '7':
            results.solver.status = SolverStatus.error
            results.solver.termination_condition = \
                TerminationCondition.error
        elif solver_status == '8':
            results.solver.status = SolverStatus.unknown
            results.solver.termination_condition = \
                TerminationCondition.unknown
        elif solver_status == '9':
            results.solver.status = SolverStatus.error
            results.solver.termination_condition = \
                TerminationCondition.solverFailure
        elif solver_status == '10':
            results.solver.status = SolverStatus.error
            results.solver.termination_condition = \
                TerminationCondition.error
        elif solver_status == '11':
            results.solver.status = SolverStatus.aborted
            results.solver.termination_condition = \
                TerminationCondition.licensingProblems
            results.solver.termination_message = \
                'Run terminated because of a licensing error.'

        if model_status == '1':
            soln.status = SolutionStatus.optimal
            results.solver.termination_condition = \
                TerminationCondition.optimal
        elif model_status == '2':
            soln.status = SolutionStatus.infeasible
            results.solver.termination_condition = \
                TerminationCondition.infeasible
        elif model_status == '3':
            soln.status = SolutionStatus.unbounded
            results.solver.termination_condition = \
                TerminationCondition.unbounded
        elif model_status == '4':
            soln.status = SolutionStatus.feasible
        elif model_status == '5':
            soln.status = SolutionStatus.unknown

        #
        # Process BARON results file
        #

        # Solutions that were preprocessed infeasible, were aborted,
        # or gave error will not have filled in res.lst files
        if results.solver.status not in [SolverStatus.error,
                                         SolverStatus.aborted]:
            #
            # Extract the solution vector and objective value from BARON
            #
            var_value = []
            var_name = []
            var_marginal = []
            con_price = []
            SolvedDuringPreprocessing = False

            #############
            #
            # Scan through the first part of the solution file, until the
            # termination message '*** Normal completion ***'
            line = ''
            while '***' not in line:
                line = INPUT.readline()
                if 'Problem solved during preprocessing' in line:
                    SolvedDuringPreprocessing = True

            INPUT.readline()
            INPUT.readline()
            try:
                objective_value = float(INPUT.readline().split()[4])
            except IndexError:
                # No objective value, so no solution to return
                if solver_status == '1' and model_status in ('1','4'):
                    logger.error(
"""Failed to process BARON solution file: could not extract the final
objective value, but BARON completed normally.  This is indicative of a
bug in Pyomo's BARON solution parser.  Please report this (along with
the Pyomo model and BARON version) to the Pyomo Developers.""")
                return
            INPUT.readline()
            INPUT.readline()

            # Scan through the solution variable values
            line = INPUT.readline()
            while line.strip() != '':
                var_value.append(float(line.split()[2]))
                line = INPUT.readline()

            # Only scan through the marginal and price values if baron
            # found that information.
            has_dual_info = False
            if 'Corresponding dual solution vector is' in INPUT.readline():
                has_dual_info = True
                INPUT.readline()
                line = INPUT.readline()
                while 'Price' not in line and line.strip() != '':
                    var_marginal.append(float(line.split()[2]))
                    line = INPUT.readline()

                if 'Price' in line:
                    line = INPUT.readline()
                    #
                    # Assume the baron_writer added the dummy
                    # c_e_FIX_ONE_VAR_CONST__ constraint as the first
                    #
                    line = INPUT.readline()
                    while line.strip() != '':
                        con_price.append(float(line.split()[2]))
                        line = INPUT.readline()

            # Skip either a few blank lines or an empty block of useless
            # marginal and price values (if 'No dual information is available')
            while 'The best solution found is' not in INPUT.readline():
                pass

            # Collect the variable names, which are given in the same
            # order as the lists for values already read
            INPUT.readline()
            INPUT.readline()
            line = INPUT.readline()
            while line.strip() != '':
                var_name.append(line.split()[0])
                line = INPUT.readline()

            assert len(var_name) == len(var_value)
            #
            #
            ################

            #
            # Plug gathered information into pyomo soln
            #

            soln_variable = soln.variable
            # After collecting solution information, the soln is
            # filled with variable name, number, and value. Also,
            # optionally fill the baron_marginal suffix
            for i, (label, val) in enumerate(zip(var_name, var_value)):

                soln_variable[label] = {"Value": val}

                # Only adds the baron_marginal key it is requested and exists
                if extract_marginals and has_dual_info:
                    soln_variable[label]["rc"] = var_marginal[i]

            # Fill in the constraint 'price' information
            if extract_price and has_dual_info:
                soln_constraint = soln.constraint
                #
                # Assume the baron_writer added the dummy
                # c_e_FIX_ONE_VAR_CONST__ constraint as the first,
                # so constraint aliases start at 1
                #
                for i, price_val in enumerate(con_price, 1):
                    # use the alias made by the Baron writer
                    con_label = ".c"+str(i)
                    soln_constraint[con_label] = {"dual": price_val}

            # This check is necessary because solutions that are
            # preprocessed infeasible have ok solver status, but no
            # objective value located in the res.lst file
            if not (SolvedDuringPreprocessing and \
                    soln.status == SolutionStatus.infeasible):
                soln.objective[objective_label] = {'Value': objective_value}

            # Fill the solution for most cases, except errors
            results.solution.insert(soln)

pyutilib.services.register_executable(name="baron")
