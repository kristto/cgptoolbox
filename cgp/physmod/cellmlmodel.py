"""
Pysundials (CVODE) wrapper for Python code autogenerated from cellml.org

Instance variable for cellml home, workspace, variable, variant
Each url-using function can take an explicit url instead
Shorter naming scheme, e.g. __main__.py in package.
{workspace}_{variant}_{exposure}

..  plot::
    
    from cgp.physmod.cellmlmodel import Cellmlmodel
    plt.title("Van der Pol Heart cell model")
    vdp = Cellmlmodel()
    t, y, flag = vdp.integrate(t=[0, 20])
    plt.plot(t, y.x, t, y.y)
"""
# pylint: disable=W0621, W0142
from tempfile import gettempdir
from StringIO import StringIO
import hashlib
from importlib import import_module
import shutil
import urllib
from collections import namedtuple
import os
import subprocess
from tempfile import NamedTemporaryFile as Tempfile
import json
from contextlib import closing
import sys
import warnings

import numpy as np
from numpy import recarray # recarray allows named columns: y.V etc.
import joblib

from ..cvodeint.namedcvodeint import Namedcvodeint
from ..utils.commands import getstatusoutput
from ..utils.dotdict import Dotdict
from cgp import physmod as cellmlmodels
from ..utils.ordereddict import OrderedDict
from ..utils.rec2dict import dict2rec
from ..utils.write_if_not_exists import write_if_not_exists
from cgp.physmod.cythonize import cythonize_model

__all__ = ["Cellmlmodel"]

_cellml2py_dir = cellmlmodels.__path__[0] + "/_cellml2py/"

mem = joblib.Memory(os.path.join(gettempdir(), "cellmlmodel"), verbose=0)

@mem.cache
def urlcache(url):
    """Cache download from URL."""
    with closing(urllib.urlopen(url)) as f:
        return f.read()

# Ensure that cellmlmodels/_cellml2py/ is a valid package directory
# This makes it easy to force re-generation of code by renaming _cellml2py/
try:
    import _cellml2py  # @UnusedImport pylint: disable=W0611
except ImportError:
    dirname, _ = os.path.split(__file__)
    with write_if_not_exists(
        os.path.join(dirname, "_cellml2py", "__init__.py")):
        pass # just create an empty __init__.py file

@mem.cache
def generate_code(url):
    """
    Generate Python code for CellML model at url. Wraps cellml-api/testCeLEDS.
    
    Written as a replacement for the code generation at models.cellml.org, 
    which was broken at the time of writing:
     
    https://tracker.physiomeproject.org/show_bug.cgi?id=3199
    
    This function encapsulates the command-line usage::
    
    cd cellml-api
    ./testCeLEDS http://models.cellml.org/workspace/tentusscher_noble_noble_panfilov_2004/@@rawfile/3e0eeae90b16221bb1ca327a5572de482990cacc/tentusscher_noble_noble_panfilov_2004_a.cellml CeLEDS/languages/Python.xml
    
    >>> print generate_code("http://models.cellml.org/workspace/"
    ... "tentusscher_noble_noble_panfilov_2004/@@rawfile/"
    ... "3e0eeae90b16221bb1ca327a5572de482990cacc/"
    ... "tentusscher_noble_noble_panfilov_2004_a.cellml")
    # Size of variable arrays:
    sizeAlgebraic = 67
    sizeStates = 17
    sizeConstants = 46
    ...
    algebraic[21] = 1125.00*exp(-pow(states[0]+27.0000, 2.00000)/240.000)+80...
    if __name__ == "__main__":
    (voi, states, algebraic) = solve_model()
    plot_model(voi, states, algebraic)
    """
    args = ["/home/jonvi/hg/cellml-api/testCeLEDS", 
            "-", 
            "/home/jonvi/hg/cellml-api/CeLEDS/languages/Python.xml"]
    with Tempfile() as cellml, Tempfile() as pycode:
        cellml.write(urlcache(url))
        cellml.seek(0)
        subprocess.call(args, stdin=cellml, stdout=pycode, stderr=subprocess.STDOUT)
        pycode.seek(0)
        return pycode.read()

Legend = namedtuple("Legend", "name component unit")

def parse_variable(s):
    """
    Parse the generated legend string for a CellML variable.
    
    >>> parse_variable("x in component Main (dimensionless)")
    Legend(name='x', component='Main', unit='dimensionless')
    """
    name, s1 = s.split(" in component ")
    component, s2 = s1.rsplit(" (")
    unit, _s3 = s2.rsplit(")")
    return Legend(name, component, unit)

def parse_legend(legend):
    """
    Parse the entry for each CellML variable in a legend list.
    
    >>> parse_legend(["x in component A (u)", "y in component B (v)"])
    Legend(name=('x', 'y'), component=('A', 'B'), unit=('u', 'v'))
    """
    L = []
    for i, s in enumerate(legend):
        if s:
            L.append(parse_variable(s))
        else:
            # This will be used as a Numpy field name and PyTables column name. 
            # The latter cannot start with double underscore in PyTables < 2.2.
            # http://www.pytables.org/trac/ticket/291
            leg = Legend(name="empty__%s" % i, component="", unit="")
            warnings.warn("Empty legend entry: %s" % (leg,))
            L.append(leg)
    L = Legend(*zip(*L))
    return L

def legend(model):
    """
    Parse the legends of a CellML model.
    
    >>> vdp = Cellmlmodel()
    >>> legend(vdp.model) # doctest: +NORMALIZE_WHITESPACE
    OrderedDict([('y', Legend(name=('x', 'y'), component=('Main', 'Main'), 
                    unit=('dimensionless', 'dimensionless'))), 
                 ('a', None), 
                 ('p', Legend(name=('epsilon',), component=('Main',), 
                    unit=('dimensionless',)))])
    
    The legend is available as an attribute of a Cellmlmodel object.
    Zipping it can be convenient sometimes.
    
    >>> zip(*vdp.legend["y"])
    [('x', 'Main', 'dimensionless'), ('y', 'Main', 'dimensionless')]
    """
    # Use OrderedDict and named tuples rather than Numpy record arrays
    # until we know the length of all strings.
    states, algebraic, _voi, constants = model.createLegends()
    legend = OrderedDict([("y", states), ("a", algebraic), ("p", constants)])
    L = [(k, parse_legend(v) if v else None) for k, v in legend.items()]
    return OrderedDict(L)

ftype = np.float64 # explicit type declaration, can be used with cython

def dup(L):
    """
    Return duplicated elements of a list.
    
    >>> dup(list("aba"))
    ['a']    
    """
    uniq = set(L)
    L = list(L[:]) # copy the list to avoid side effects
    for i in uniq:
        L.remove(i)
    return L

def dtypes(legend):
    """
    Return Numpy data types for a CellML model; object with attributes y, p, a.   
    
    The result is a Dotdict, whose keys can be used as attributes.

    >>> from cgp.physmod.cellmlmodel import Cellmlmodel
    >>> vdp = Cellmlmodel()
    >>> d = dtypes(legend(vdp.model))
    
    Now, d.y, d.p, d.a are dtype for state, parameters or "algebraic" variables.
    
    >>> d.y
    dtype([('x', '<f8'), ('y', '<f8')])
    
    >>> d
    Dotdict({'a': None,
     'p': dtype([('epsilon', '<f8')]),
     'y': dtype([('x', '<f8'), ('y', '<f8')])})
    """
    # Dict of duplicates in each legend item (states, algebraics, parameters)
    d = dict((k, dup(v.name)) for k, v in legend.items() if v)
    d = dict((k, v) for k, v in d.items() if v) # drop items with no duplicates
    if d: # duplicate names, must be disambiguated
        import copy
        L = copy.deepcopy(legend)
        warnings.warn("Duplicate names: %s" % d)
        for k, dupnames in d.items():
            name = list(L[k].name) # convert tuple to mutable list
            # append __i to each duplicate item, where i is its index
            for i, n in enumerate(name):
                if n in dupnames:
                    name[i] = "%s__%s" % (n, i)
            assert not dup(name), "Failed to disambiguate names: %s" % d
            L[k] = L[k]._replace(name=tuple(name))  # pylint: disable=W0212
    else: # no duplicates, use legend as is
        L = legend
    # Generate data type for each legend item
    DT = [(k, np.dtype([(n, ftype) for n in v.name]) if v else None) 
        for k, v in L.items()]
    return Dotdict(DT)

#: Python code appended to that which is autogenerated from CellML
py_addendum = '''
### Added by cellmlmodel.py ###

# @todo: The following module-level variables are shared across instances.
#        It might be better to wrap them in a class, allowing each instance of 
#        the same model to have its own parameter vector.

import sys
import numpy as np

ftype = np.float64 # explicit type declaration, can be used with cython
y0 = np.zeros(sizeStates, dtype=ftype)
ydot = np.zeros(sizeStates, dtype=ftype)
p = np.zeros(sizeConstants, dtype=ftype)
algebraic = np.zeros(sizeAlgebraic, dtype=ftype)

y0[:], p[:] = initConsts()

exc_info = None

# Sundials calling convention: https://computation.llnl.gov/casc/sundials/documentation/cv_guide/node6.html#SECTION00661000000000000000

def ode(t, y, ydot, f_data):
    """
    Compute rates of change for differential equation model.
    
    Rates are written into ydot[:]. 
    f_data is ignored, but required by the CVODE interface.
    
    The function returns 0 on success and -1 on failure.
    
    >>> ode(None, None, None, None)
    -1
    
    For debugging in case of failure, exception info is stored in the 
    module-level variable exc_info. (The message ends in "unsubscriptable" 
    under Python 2.6 but "not subscriptable" under Python 2.7, hence the 
    ellipsis.) Unfortunately, this is currently not implemented in a compiled 
    ODE. It will check the type of arguments before executing, but I am not 
    sure what happens in case of run-time errors inside the ODE.
    
    >>> exc_info
    (<type 'exceptions.TypeError'>,
    TypeError("'NoneType' object is ...subscriptable",),
    <traceback object at 0x...>)
    """
    global exc_info
    exc_info = None
    try:
        ydot[:] = computeRates(t, y, p)
        return 0
    except StandardError:
        import sys
        exc_info = sys.exc_info()
        return -1

def rates_and_algebraic(t, y):
    """
    Compute rates and algebraic variables for a given state trajectory.
    
    Unfortunately, the CVODE machinery does not offer a way to return rates and 
    algebraic variables during integration. This function re-computes the rates 
    and algebraics at each time step for the given state.
    
    This returns a simple float array; 
    :meth:`cgp.physmod.cellmlmodel.Cellmlmodel.rates_and_algebraic`
    will cast them to structured arrays with named fields.
    
    This version is pure Python; 
    :func:`~cgp.physmod.cythonize.cythonize`
    will generate a faster version.
    """
    imax = len(t)
    # y can be NVector, unstructured or structured Numpy array.
    # If y is NVector, its data will get copied into a Numpy array.
    y = np.array(y).view(float)
    ydot = np.zeros_like(y)
    alg = np.zeros((imax, len(algebraic)))
    for i in range(imax):
        ydot[i] = computeRates(t[i], y[i], p)
        if len(algebraic):
            # need np.atleast_1d() because computeAlgebraic() uses len(t)
            alg[i] = computeAlgebraic(p, y[i], np.atleast_1d(t[i])).squeeze()
    return ydot, alg
'''

@mem.cache
def guess_url(self, urlpattern="exposure/{exposure}/{variant}.cellml/"):
    """
    >>> class Test(Cellmlmodel):
    ...     def __init__(self, **kwargs):
    ...         self.__dict__.update(kwargs)
    >>> guess_url(Test(workspace="bondarenko_szigeti_bett_kim_rasmusson_2004",
    ...     exposure=None, changeset=None, variant=None))
    'http://models.cellml.org/exposure/11df840d0150d34c9716cd4cbdd164c8/bondarenko_szigeti_bett_kim_rasmusson_2004_apical.cellml/'
    """
    if not self.exposure:
        self.exposure = self.get_latest_exposure()
    if not self.variant:
        self.variants = self.get_variants()
        self.variant = self.variants[0]
    if not self.changeset:
        self.changeset = self.get_changeset()
    return self.cellml_home + urlpattern.format(**self.__dict__)

class Cellmlmodel(Namedcvodeint):
    """
    Class to solve CellML model equations.

    ..  plot::
        
        from cgp.physmod.cellmlmodel import Cellmlmodel
        vdp = Cellmlmodel() # default van der Pol model
        t, Yr, flag = vdp.integrate(t=[0, 20])
        plt.plot(t,Yr.view(float))
    
    The constructor will download autogenerated Python code from cellml.org if
    possible, and otherwise look for a corresponding .py.orig file in the
    cgp/physmod/_cellml2py/ directory. This code is wrapped to be compatible with
    CVode, and saved as a .py file in the same directory.
    
    If ``use_cython=True`` (the default), the code is rewrapped for `Cython
    <http://www.cython.org>`_ and compiled for speed. Compiled models reside in
    cgp/physmod/_cellml2py/cython/ where each compiled model has a subdirectory
    containing several files. The .pyx file and setup.py file can be tweaked by
    hand if required, and manually recompiled by changing to that directory and
    running python setup.py build_ext --inplace The compiled module has
    extension .so (Linux) or .pyd (Windows).
        
    Compiling the model causes some minor differences in behaviour, see
    :func:`~cgp.test.test_cellmlmodel.test_compiled_behaviour` for details.
    """
    cellml_home = "http://models.cellml.org/"
    # cellml_home = "http://184.169.251.126/"
    cellml_home = cellml_home.rstrip("/") + "/"
    
    def __init__(self,  # pylint: disable=W0102,E1002,R
        url="http://models.cellml.org/workspace/vanderpol_vandermark_1928/"
        "@@rawfile/371151b156888430521cbf15a9cfa5e8d854cf37/"
        "vanderpol_vandermark_1928.cellml",
        workspace=None, exposure=None, changeset=None, variant=None,  
        t=[0, 1], y=None, p=None, rename={}, use_cython=True, purge=False, 
        **kwargs):
        """
        Wrap autogenerated CellML->Python for use with pysundials
        
        M = Cellmlmodel(workspace, exposure, variant) downloads and caches 
        Python code autogenerated for the CellML model identified by the 
        (workspace, exposure, variant) triple,  and wraps it in a class with 
        convenience attributes and functions for use with pysundials.
        
        Defaults are the latest *exposure* and the first *variant* listed at 
        the cellml.org site. 

        If the non-wrapped Python code is in a local file, 
        e.g. exported from OpenCell, http://www.cellml.org/tools/opencell/ 
        use the "file://" protocol.
        
        >>> newmodel = Cellmlmodel("/newmodel", "file:c:/temp/exported.py")
        ... # doctest: +SKIP
        
        Here, "newmodel" is whatever name you'd like for the wrapper module, 
        and "exported.py" is whatever name you saved the exported code under.
        (Strictly speaking, the URL should be "file:///c:/temp/exported.py", 
        but the simpler version is also accepted by urllib.urlopen().)
        
        The constructor arguments are as follows:
        
        TODO: Update this to use (workspace, exposure, variant) as identifiers.
        
        exposure_workspace: identifiers in the repository at cellml.org,
        e.g. "732c32162c845016250f234416415bfc7601f41c/vanderpol_vandermark_1928_version01"
        for http://models.cellml.org/exposure/2224a49c6b39087dad8682648658775d.
        If only the workspace is given, will try to obtain the latest 
        workspace from the repository.
        
        urlpattern : URL to non-wrapped Python code for model, with
        %(workspace)s and %(exposure)s placeholders for e.g.
        732c32162c845016250f234416415bfc7601f41c
        vanderpol_vandermark_1928_version01
        
        t, y : as for Cvodeint
        
        p : optional parameter vector
        
        purge : (re-)download model even if the file is already present?
        
        rename : e.g. dict with possible keys "y", "p", "a", whose values are 
        mappings for renaming variables. You should rarely need this, but it is 
        useful to standardize names of parameters to be manipulated, see e.g. 
        ap_cvode.Tentusscher.__init__().
        
        use_cython: if True, wrap the model for Cython and compile.
        Cython files are placed in cgp/physmod/_cellml2py/cython/modulename/, 
        and cgp.physmod._cellml2py.cython.modulename.modulename is used 
        in place of cgp.physmod._cellml2py.modulename.
        
        >>> Cellmlmodel().dtype
        Dotdict({'a': None,
         'p': dtype([('epsilon', '<f8')]),
         'y': dtype([('x', '<f8'), ('y', '<f8')])})
        >>> Cellmlmodel(rename={"y": {"x": "V"}, "p": {"epsilon": "newname"}}).dtype
        Dotdict({'a': None,
         'p': dtype([('newname', '<f8')]),
         'y': dtype([('V', '<f8'), ('y', '<f8')])})
        
        See class docstring: ?Cellmlmodel for details.
        """
        self.workspace = workspace
        self.exposure = exposure
        self.changeset = changeset
        self.variant = variant
        self.url = url or guess_url(self)
        self.cellml = urlcache(url)
        self.hash = "_" + hashlib.sha1(self.cellml).hexdigest()[:6]
        self.package = "cgp.physmod._cellml2py." + self.hash
        self.packagedir = _cellml2py_dir + self.hash
        if purge:
            try:
                shutil.rmtree(self.packagedir)
            except WindowsError:
                pass
        if use_cython:
            self._import_cython()
        else:
            self._import_python()
        if y is None:
            y = self.model.y0
        self.legend = legend(self.model)
        dtype = dtypes(self.legend)
        # Rename fields if requested
        for i in "a", "y", "p":
            if i in rename:
                L = eval(str(dtype[i]))
                for j, (nam, typ) in enumerate(L):
                    if nam in rename[i]:
                        L[j] = (rename[i][nam], typ)
                dtype[i] = np.dtype(L)
        # if there are no parameters or algebraic variables, make empty recarray
        try:
            pr = self.model.p.view(dtype.p).view(recarray)
        except TypeError:
            pr = np.array([]).view(recarray)
        try:
            self.algebraic = self.model.algebraic.view(dtype.a).view(recarray)
        except TypeError:
            self.algebraic = np.array([]).view(recarray)
        self.y0r = self.model.y0.view(dtype.y).view(recarray)
        super(Cellmlmodel, self).__init__(self.model.ode, t, 
            y.view(dtype.y), pr, **kwargs)
        assert all(dtype[k] == self.dtype[k] for k in self.dtype)
        self.dtype.update(dtype)
        self.originals["y0r"] = self.y0r
        if p:
            self.model.p[:] = p
    
    def _import_python(self):
        py_file = os.path.join(self.packagedir, "py.py")
        try:
            self.model = import_module(".py", self.package)
        except ImportError:
            with write_if_not_exists(os.path.join(self.packagedir, 
                                                  "__init__.py")) as f:
                pass  # just create empty __init__.py to make a package
            with write_if_not_exists(os.path.join(self.packagedir, 
                self.url.rsplit("/", 1)[-1])) as f:
                f.write(urlcache(self.url))
            with write_if_not_exists(py_file) as f:
                f.write(urlcache("http://bebiservice.umb.no/bottle/cellml2py/" + self.url))
            self.model = import_module(".py", self.package)
        try:
            with open(py_file, "rU") as f:
                self.py_code = f.read()
        except IOError:
            self.py_code = "Source file open failed"
    
    def _import_cython(self):
        self._import_python()
        try:
            self.model = import_module(".cy", self.package)
        except ImportError:
            self.model = self.cythonize()
    
    s = """
        if localfile:
            self.workspace = self.exposure = self.variant = None
            self.name = localfile
        else:
            self.cellml_home = cellml_home.rstrip("/") + "/"
            self.workspace = workspace
            self.exposure = exposure or self.get_latest_exposure()
            if variant:
                self.variant = variant
            else:
                variants = self.get_variants()
                if variants:
                    self.variant = variants[0]
                else:
                    self.variant = ""
            self.changeset = changeset or self.get_changeset()
            self.name = urllib.quote(
                self.workspace + self.exposure + self.variant, safe="")
        modelfilename = cellmlmodels.__path__[0]
        modelfilename += '/_cellml2py/' + self.name + ".py"
        modulename = 'cgp.physmod._cellml2py.' + self.name
        # import the model module, creating it if necessary

            # try to download Python code autogenerated from CellML
            # url = self.cellml_home + codegenpattern.format(**self.__dict__)
            # Hack to work around bug in CellML code generation for Python
            url = "http://bebiservice.umb.no/bottle/ccgs/" + self.cellml_home
            url += ("workspace/{workspace}/@@rawfile/"
                "{changeset}/{variant}.cellml").format(**self.__dict__)
            origfile = modelfilename + ".orig"
            try:
                py_code = urlcache(url)
            except IOError:
                py_code = ""
            if "computeRates" in py_code:
                # save original file for later reference, e.g. cythonization
                with open(origfile, "w") as f:
                    f.write(py_code)
            else:
                if localfile:
                    with open(origfile) as f:
                        py_code = f.read()
                else:
                    # look for existing .py.orig file in cgp/physmod/_cellml2py
                    msg = ("Failed to get autogenerated Python code.\n" +
                        ("* Reading from URL failed" if not py_code else (
                        "* URL did not provide valid Python code. Got:\n\n" + 
                        "\n".join(py_code.strip().split("\n")[:3]) + "\n...")) +
                        "\n{}\n" + 
                        "If you have a .cellml file, try exporting it to Python " +
                        "using OpenCell and save it under the 'local file' name " +
                        "given below.\nOpenCell: " +
                        "http://www.cellml.org/tools/downloads/opencell/releases/" +
                        "\nURL: " + url + "\nLocal file: " + origfile)
                    try:
                        with open(origfile) as f:
                            py_code = f.read()
                    except IOError:
                        raise IOError(msg.format(
                            "\n* Local .py.orig file does not exist."))
                    assert "computeRates" in py_code, msg.format(
                        "Local file does exist, but Python code is not valid.")
"""
    
    def save_legend(self, *args, **kwargs):
        """
        Save :data:`legend` for CellML model as CSV.
        
        Arguments are passed to :func:`matplotlib.mlab.rec2csv`.
        
        >>> from cStringIO import StringIO
        >>> from cgp.physmod.cellmlmodel import Cellmlmodel
        >>> vdp = Cellmlmodel()
        >>> sio = StringIO()
        >>> vdp.save_legend(sio)
        >>> sio.getvalue().split()
        ['role,name,component,unit',
         'y,x,Main,dimensionless',
         'y,y,Main,dimensionless',
         'p,epsilon,Main,dimensionless']
        """
        import matplotlib.mlab  # deferred import to minimize dependencies
        
        L = [(k, n, c, u) for k, v in self.legend.items() if v 
             for n, c, u in zip(*v)]
        flat_legend = np.rec.fromrecords(L, 
            names="role name component unit".split())
        matplotlib.mlab.rec2csv(flat_legend, *args, **kwargs)
    
    def rates_and_algebraic(self, t, y, par=None):
        """
        Compute rates and algebraic variables for a given state trajectory.
        
        Unfortunately, the CVODE machinery does not offer a way to return rates and 
        algebraic variables during integration. This function re-computes the rates 
        and algebraics at each time step for the given state.
        
        ..  plot::
            
            from cgp.physmod.cellmlmodel import Cellmlmodel
            bond = Cellmlmodel(
                "bondarenko_szigeti_bett_kim_rasmusson_2004_apical", t=[0, 20])
            bond.yr.V = 100  # simulate stimulus
            t, y, flag = bond.integrate()
            ydot, alg = bond.rates_and_algebraic(t, y)
            plt.plot(t, alg.J_xfer, '.-', t, y.Cai, '.-')
        """
        m = self.model
        t = np.atleast_1d(t).astype(float)
        y = np.atleast_2d(y)
        # y = y.view(ftype) # done already in rates_and_algebraic
        with self.autorestore(_p=par):
            ydot, alg = m.rates_and_algebraic(t, y)
        # ydot = ydot.view(self.dtype.y, np.recarray).squeeze()
        # alg = alg.view(self.dtype.a, np.recarray).squeeze()
        ydot = ydot.squeeze().view(self.dtype.y, np.recarray)
        alg = alg.squeeze().view(self.dtype.a, np.recarray)
        return ydot, alg
    
    def cythonize(self):
        """
        Return Cython code for this model (further hand-tweaking may be needed).
        
        This just imports and calls 
        :func:`cgp.physmod.cythonize.cythonize_model`.
        """
        modulename_cython = self.package + ".cy"
        modelname = self.hash
        modelfilename = os.path.join(self.packagedir, "cy.pyx")
        try:
            __import__(modulename_cython)
            return sys.modules[modulename_cython]
        except ImportError:
            pyx, setup = cythonize_model(self.py_code, modelname)
            pyxname = modelfilename.replace("%s.py" % modelname, 
                "cython/%s/m.pyx" % modelname)
            dirname, _ = os.path.split(pyxname)
            setupname = os.path.join(dirname, "setup.py")
            # make the cython and model directories "packages"
            cyinitname = os.path.join(dirname, os.pardir, "__init__.py")
            modelinitname = os.path.join(dirname, "__init__.py")
            with write_if_not_exists(cyinitname):
                pass # just create an empty __init__.py file
            with write_if_not_exists(modelinitname):
                pass # just create an empty __init__.py file
            with open(pyxname, "w") as f:
                f.write(pyx)
            with open(setupname, "w") as f:
                f.write(setup)
            cmd = "python setup.py build_ext --inplace"
            status, output = getstatusoutput(cmd, cwd=dirname)
            # Apparently, errors fail to cause status != 0.
            # However, output does include any error messages.
            if "cannot find -lsundials_cvode" in output:
                raise OSError("Cython-compilation of ODE right-hand side "
                    "failed because SUNDIALS was not found.\n"
                    "Status code: %s\nCommand: %s\n"
                    "Output (including errors):\n%s" % (status, cmd, output))
            if status != 0:
                raise RuntimeError("'%s'\nreturned status %s:\n%s" % 
                    (cmd, status, output))
            try:
                __import__(modulename_cython)
                return sys.modules[modulename_cython]
            except StandardError, exc:
                raise ImportError("Exception raised: %s: %s\n\n"
                    "Cython compilation may have failed. "
                    "The compilation command was:\n%s\n\n"
                    "The output of the compilation command was:\n%s"
                    % (exc.__class__.__name__, exc, cmd, output))
    
    def makebench(self):
        """
        Return IPython code for benchmarking compiled vs. uncompiled ode.
        
        >>> print Cellmlmodel().makebench()
        # Run the following from the IPython prompt:
        import os
        import cgp.physmod... as m
        reload(m)
        from utils.capture_output import Capture
        with Capture() as cap:
            print "##### Timing the current version #####"
            timeit m.ode(0, m.y0, m.ydot, None)
        ...
        
        Executing this in IPython gives something like this for a model whose 
        right-hand-side module has been compiled.
        ##### Timing the current version #####
        10000 loops, best of 3: 22.7 us per loop
        ##### Timing pure Python version #####
        100 loops, best of 3: 2.28 ms per loop        
        """
        template = """# Run the following from the IPython prompt:
import os
import %s as m
reload(m)
from utils.capture_output import Capture
with Capture() as cap:
    print "##### Timing the current version #####" 
    timeit m.ode(0, m.y0, m.ydot, None)

src = m.__file__
_, ext = os.path.splitext(src)
if ext in [".so", ".pyd"]:
    backup = src + ".backup"
    os.rename(src, backup)
    try:
        reload(m)
        with cap:
            print "##### Timing pure Python version #####"
            timeit m.ode(0, m.y0, m.ydot, None)
    
    finally:
        os.rename(backup, src)
        print "##### Restored compiled version #####"
        reload(m)

print cap
"""
        return template % self.model.__name__
    
    def get_latest_exposure(self, fmt="workspace/{workspace}"):
        """
        Get latest exposure from models.cellml.org.
        
        :param str workspace: A cellml.org workspace identifier, as returned by 
            :func:`get_all_workspaces`.
        
        >>> class Test(Cellmlmodel):
        ...     def __init__(self):
        ...         self.workspace = "bondarenko_szigeti_bett_kim_rasmusson_2004"
        >>> Test().get_latest_exposure()
        '11df840d0150d34c9716cd4cbdd164c8'
        >>> class Test(Cellmlmodel):
        ...     def __init__(self):
        ...         self.workspace = "beeler_reuter_1977"
        >>> Test().get_latest_exposure()
        'e/9a'
        """
        # Scan for "Latest Exposure" link.
        # The href is direct for the beeler_reuter_1977 model; 
        # others are prefixed with "exposure/"
        from lxml import etree
        url = self.cellml_home + fmt.format(**self.__dict__)
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(StringIO(urlcache(url)), parser)
        query = ('//{http://www.w3.org/1999/xhtml}' +
            'a[text()="Latest Exposure"]/@href')
        try:
            href = etree.ETXPath(query)(tree)[0]
            href = href[len(self.cellml_home):]
            if href.startswith("exposure/"):
                return href[len("exposure/"):]
            else:
                return href
        except IndexError:
            raise IOError('Failed to extract "Latest Exposure" href at {}'.format(url))
    
    def get_changeset(self, fmt="exposure/{exposure}/{variant}.cellml/view"):
        """
        >>> class Test(Cellmlmodel):
        ...     def __init__(self):
        ...         self.workspace = "vanderpol_vandermark_1928"
        ...         self.exposure = "5756af26cfb20a7f66a51f66af10a70a"
        ...         self.variant = "vanderpol_vandermark_1928"
        >>> test = Test()
        >>> test.get_changeset()
        '371151b156888430521cbf15a9cfa5e8d854cf37'
        """
        from lxml import etree
        url = self.cellml_home + fmt.format(**self.__dict__)
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(StringIO(urlcache(url)), parser)
        query = ('//{http://www.w3.org/1999/xhtml}' +
            'a[text()="Download This File"]/@href')
        try:
            return etree.ETXPath(query)(tree)[0].rsplit("/", 2)[-2]
        except IndexError:
            raise IOError('Found no "Download This File" link at {}'.format(url))
    
    def get_variants(self, fmt="exposure/{exposure}"):
        """
        List the variants of a CellML model in an exposure.
        
        >>> class Test(Cellmlmodel):
        ...     def __init__(self):
        ...         self.workspace = "bondarenko_szigeti_bett_kim_rasmusson_2004"
        ...         self.exposure = self.get_latest_exposure()
        >>> Test().get_variants()
        ['bondarenko_szigeti_bett_kim_rasmusson_2004_apical',
         'bondarenko_szigeti_bett_kim_rasmusson_2004_septal']
        """
        from lxml import etree
        url = self.cellml_home + fmt.format(**self.__dict__)
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(StringIO(urlcache(url)), parser)
        query = ('//{http://www.w3.org/1999/xhtml}' +
            'a[contains(@class, "contenttype-exposurefile")]')
        el = etree.ETXPath(query)(tree)
        return [e.attrib["href"].rsplit("/", 2)[-2][:-len(".cellml")] for e in el]

def test_cellmlmodel():
    """
    >>> c = Cellmlmodel("http://models.cellml.org/workspace/tentusscher_noble_noble_panfilov_2004/@@rawfile/3e0eeae90b16221bb1ca327a5572de482990cacc/tentusscher_noble_noble_panfilov_2004_a.cellml", use_cython=False, purge=True)
    >>> c
    Cellmlmodel(url='http://models.cellml.org/workspace/tentusscher_noble_noble_panfilov_2004/@@rawfile/3e0eeae90b16221bb1ca327a5572de482990cacc/tentusscher_noble_noble_panfilov_2004_a.cellml', y=[-86.2, 138.3, 11.6, 0.0002, 0.0, 1.0, 0.0, 0.0, 0.75, 0.75, 0.0, 1.0, 1.0, 1.0, 0.0, 0.2, 1.0])
    >>> c.model
    <module 'cgp.physmod._cellml2py._f09947.py' from 'C:\git\cgptoolbox\cgp\physmod\_cellml2py\_f09947\py.py'>
    """
    pass

def get_all_workspaces(get_latest_exposures=False,
    url="http://models.cellml.org/workspace/rest/contents.json"):
    """
    List all available CellML models.
    
    :param bool get_latest_exposures: Query models.cellml.org for latest 
        exposure for each model? This can take several minutes.
    :param str url: URL to list of cellml.org workspaces.
    :return: Record array with fields "title", "uri", "workspace",
        and optionally "exposure", "ew", where the latter can be passed to the 
        :meth:`Cellmlmodel` constructor.
    
    If you wanted to check out every workspace at cellml.org, 
    you could run the following in IPython::
    
        from cgp.physmod.cellmlmodels import get_all_workspaces
        w = get_all_workspaces()
        for uri in w.uri:
            !hg clone $uri
    """
    d = json.loads(urlcache(url))
    w = OrderedDict((k.lower(), v) 
                    for k, v in zip(d["keys"], zip(*d["values"])))
    w["workspace"] = [i.split("/")[-1] for i in w["uri"]]
    if get_latest_exposures:
        m = Cellmlmodel()
        w["exposure"] = []
        for ws in w["workspace"]:
            m.workspace = ws
            w["exposure"].append(m.get_latest_exposure())
    return dict2rec(w).view(np.recarray)
    
if __name__ == "__main__":
    import doctest
    failure_count, test_count = doctest.testmod(optionflags=
        doctest.NORMALIZE_WHITESPACE | doctest.ELLIPSIS | 
        doctest.REPORT_ONLY_FIRST_FAILURE)
    print """
        NOTE: You may see AttributeError when pysundials tries to __del__
        NVector objects that are None. This is probably not a problem.
        
        Also, bugs in the CellML code generation cause a few warnings for the 
        Bondarenko model. 
        """
    if failure_count == 0:
        print """
            All doctests passed.
            """
