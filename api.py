#!/usr/bin/env python

import webapp2
import jinja2
import re
from google.appengine.ext import ndb
from google.appengine.ext import blobstore
from google.appengine.api import users
from google.appengine.ext.webapp import blobstore_handlers
import logging
import parsers
import headers
import os

logging.basicConfig(level=logging.INFO) # dev_appserver.py --log_level debug .
log = logging.getLogger(__name__)

SCHEMA_VERSION="1.8.1"

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
    extensions=['jinja2.ext.autoescape'], autoescape=True)

ENABLE_JSONLD_CONTEXT = True

# Core API: we have a single schema graph built from triples and units.

NodeIDMap = {}
DataCache = {}
SHOWPREFIXES = True
PrefixMap = {}
VersionList = []

class Unit ():
    """
    Unit represents a node in our schema graph. IDs are local,
    e.g. "Person" or use simple prefixes, e.g. rdfs:Class.
    """

    def __init__ (self, id):
        self.id = id
        self.prefix = ""
        NodeIDMap[id] = self
        self.arcsIn = []
        self.arcsOut = []
        self.examples = []
        self.subtypes = None

    @staticmethod
    def GetUnit (id, createp=False):
        """Return a Unit representing a node in the schema graph.

        Argument:
        createp -- should we create node if we don't find it? (default: False)
        """
        if (id in NodeIDMap):
            return NodeIDMap[id]
        if (createp != False):
            return Unit(id)

    def typeOf(self, type):
        """Boolean, true if the unit has an rdf:type matching this type."""
        for triple in self.arcsOut:
            if (triple.target != None and triple.arc.id == "typeOf"):
                val = triple.target.subClassOf(type)
                if (val):
                    return True
        return False

    def subClassOf(self, type):
        """Boolean, true if the unit has an rdfs:subClassOf matching this type."""
        if (self.id == type.id):
            return True
        for triple in self.arcsOut:
            if (triple.target != None and triple.arc.id == "rdfs:subClassOf"):
                val = triple.target.subClassOf(type)
                if (val):
                    return True
        return False

    def isClass(self):
        """Does this unit represent a class/type?"""
        return self.typeOf(Unit.GetUnit("rdfs:Class"))

    def isAttribute(self):
        """Does this unit represent an attribute/property?"""
        return self.typeOf(Unit.GetUnit("rdf:Property"))

    def isEnumeration(self):
        """Does this unit represent an enumerated type?"""
        return self.subClassOf(Unit.GetUnit("Enumeration"))

    def isEnumerationValue(self):
        """Does this unit represent a member of an enumerated type?"""
        types = GetTargets(Unit.GetUnit("typeOf"), self  )
        log.debug("isEnumerationValue() called on %s, found %s types." % (self.id, str( len( types ) )) )
        found_enum = False
        for t in types:
          if t.subClassOf(Unit.GetUnit("Enumeration")):
            found_enum = True
        return found_enum

    def isDataType(self):
      """
      Does this unit represent a DataType type or sub-type?

      DataType and its children do not descend from Thing, so we need to
      treat it specially.
      """
      return self.subClassOf(Unit.GetUnit("DataType"))

    def superceded(self):
        """Has this property been superceded? (i.e. deprecated/archaic)"""
        for triple in self.arcsOut:
            if (triple.target != None and triple.arc.id == "supercededBy"):
                return True
        return False

    def supercedes(self):
        """Returns a property that supercedes this one, or nothing."""
        for triple in self.arcsIn:
            if (triple.source != None and triple.arc.id == "supercededBy"):
                return triple.source
        return None

    def superproperties(self):
        """Returns super-properties of this one."""
        if not self.isAttribute():
          logging.debug("Non-property %s won't have superproperties." % self.id)
        superprops = []
        for triple in self.arcsOut:
            if (triple.target != None and triple.arc.id == "rdfs:subPropertyOf"):
                superprops.append(triple.target)
        return superprops

    def subproperties(self):
        """Returns direct subproperties of this property."""
        if not self.isAttribute():
          logging.debug("Non-property %s won't have subproperties." % self.id)
          return None
        subprops = []
        for triple in self.arcsIn:
            if (triple.source != None and triple.arc.id == "rdfs:subPropertyOf"):
              subprops.append(triple.source)
        return subprops

    def inverseproperty(self):
        """A property that is an inverseOf this one, e.g. alumni vs alumniOf."""
        for triple in self.arcsOut:
            if (triple.target != None and triple.arc.id == "inverseOf"):
               return triple.target
        for triple in self.arcsIn:
            if (triple.source != None and triple.arc.id == "inverseOf"):
               return triple.source
        return None

    def setPrefix(self,path):
        if(self.prefixes()):
            self.prefix = path
            if(path.endswith(self.id)):
                self.prefix = path[:len(path) - len(self.id)]
                self.prefix = PrefixMap.get(self.prefix,self.prefix)

    @staticmethod
    def storeVersion(ver):
        if(ver not in VersionList):
            VersionList.append(ver)

#Prefix from rdfa files in form "prefix:path"
    @staticmethod
    def storePrefix(pf):
        sp = pf.strip().split(':',1)
        PrefixMap[sp[1]] = sp[0]

    def setPrefix(self,path):
        if(self.prefixes()):
            self.prefix = path
            if(path.endswith(self.id)):
                self.prefix = path[:len(path) - len(self.id)]
                self.prefix = PrefixMap.get(self.prefix,self.prefix)

    def getPrefix(self):
        if(self.prefixes()):
            return self.prefix
        return "" 

    @staticmethod
    def prefixes():
        if(not SHOWPREFIXES or len(PrefixMap) <= 1):
             return False
        return True

    @staticmethod
    def pathForPrefix(prefix):
        for path, pre in PrefixMap.iteritems():
            if(pre == prefix):
                return path
        return ""


class Triple () :

    def __init__ (self, source, arc, target, text):
        """Triple constructor keeps state via source node's arcsOut."""
        self.source = source
        source.arcsOut.append(self)
        self.arc = arc

        if (target != None):
            self.target = target
            self.text = None
            target.arcsIn.append(self)
        elif (text != None):
            self.text = text
            self.target = None

    @staticmethod
    def AddTriple(source, arc, target):
        """AddTriple stores a thing-valued new Triple within source Unit."""
        if (source == None or arc == None or target == None):
            return
        else:
            return Triple(source, arc, target, None)

    @staticmethod
    def AddTripleText(source, arc, text):
        """AddTriple stores a string-valued new Triple within source Unit."""
        if (source == None or arc == None or text == None):
            return
        else:
            return Triple(source, arc, None, text)

def GetTargets(arc, source):
    """All values for a specified arc on specified graph node."""
    targets = {}
    for triple in source.arcsOut:
        if (triple.arc == arc):
            if (triple.target != None):
                targets[triple.target] = 1
            elif (triple.text != None):
                targets[triple.text] = 1
    return targets.keys()

def GetSources(arc, target):
    """All source nodes for a specified arc pointing to a specified node."""
    sources = {}
    for triple in target.arcsIn:
        if (triple.arc == arc):
            sources[triple.source] = 1
    return sources.keys()

def GetArcsIn(target):
    """All incoming arc types for this specified node."""
    arcs = {}
    for triple in target.arcsIn:
        arcs[triple.arc] = 1
    return arcs.keys()

def GetArcsOut(source):
    """All outgoing arc types for this specified node."""
    arcs = {}
    for triple in source.arcsOut:
        arcs[triple.arc] = 1
    return arcs.keys()

# Utility API

def GetComment(node) :
    """Get the first rdfs:comment we find on this node (or "No comment")."""
    for triple in node.arcsOut:
        if (triple.arc.id == 'rdfs:comment'):
            return triple.text
    return "No comment"

def GetImmediateSubtypes(n):
    """Get this type's immediate subtypes, i.e. that are subClassOf this."""
    if n==None:
        return None
    subs = GetSources( Unit.GetUnit("rdfs:subClassOf"), n)
    return subs

def GetImmediateSupertypes(n):
    """Get this type's immediate supertypes, i.e. that we are subClassOf."""
    if n==None:
        return None
    return GetTargets( Unit.GetUnit("rdfs:subClassOf"), n)

def GetAllTypes():
    """Return all types in the graph."""
    if DataCache.get('AllTypes'):
        logging.debug("DataCache HIT: Alltypes")
        return DataCache.get('AllTypes')
    else:
        logging.debug("DataCache MISS: Alltypes")
        mynode = Unit.GetUnit("Thing")
        subbed = {}
        todo = [mynode]
        while todo:
            current = todo.pop()
            subs = GetImmediateSubtypes(current)
            subbed[current] = 1
            for sc in subs:
                if subbed.get(sc.id) == None:
                    todo.append(sc)
        DataCache['AllTypes'] = subbed.keys()
        return subbed.keys()

def GetParentList(start_unit, end_unit=None, path=[]):

        """
        Returns one or more lists, each giving a path from a start unit to a supertype parent unit.

        example:

        for path in GetParentList( Unit.GetUnit("Restaurant") ):
            pprint.pprint(', '.join([str(x.id) for x in path ]))

        'Restaurant, FoodEstablishment, LocalBusiness, Organization, Thing'
        'Restaurant, FoodEstablishment, LocalBusiness, Place, Thing'
        """

        if not end_unit:
          end_unit = Unit.GetUnit("Thing")

        arc=Unit.GetUnit("rdfs:subClassOf")
        logging.debug("from %s to %s - path length %d" % (start_unit.id, end_unit.id, len(path) ) )
        path = path + [start_unit]
        if start_unit == end_unit:
            return [path]
        if not Unit.GetUnit(start_unit.id):
            return []
        paths = []
        for node in GetTargets(arc, start_unit):
            if node not in path:
                newpaths = GetParentList(node, end_unit, path)
                for newpath in newpaths:
                    paths.append(newpath)
        return paths

def HasMultipleBaseTypes(typenode):
    """True if this unit represents a type with more than one immediate supertype."""
    return len( GetTargets( Unit.GetUnit("rdfs:subClassOf"), typenode ) ) > 1

class Example ():

    @staticmethod
    def AddExample(terms, original_html, microdata, rdfa, jsonld):
       """
       Add an Example (via constructor registering it with the terms that it
       mentions, i.e. stored in term.examples).
       """
       # todo: fix partial examples: if (len(terms) > 0 and len(original_html) > 0 and (len(microdata) > 0 or len(rdfa) > 0 or len(jsonld) > 0)):
       if (len(terms) > 0 and len(original_html) > 0 and len(microdata) > 0 and len(rdfa) > 0 and len(jsonld) > 0):
            return Example(terms, original_html, microdata, rdfa, jsonld)

    def get(self, name) :
        """Exposes original_content, microdata, rdfa and jsonld versions."""
        if name == 'original_html':
           return self.original_html
        if name == 'microdata':
           return self.microdata
        if name == 'rdfa':
           return self.rdfa
        if name == 'jsonld':
           return self.jsonld

    def __init__ (self, terms, original_html, microdata, rdfa, jsonld):
        """Example constructor, registers itself with the relevant Unit(s)."""
        self.terms = terms
        self.original_html = original_html
        self.microdata = microdata
        self.rdfa = rdfa
        self.jsonld = jsonld
        for term in terms:
            term.examples.append(self)



def GetExamples(node):
    """Returns the examples (if any) for some Unit node."""
    return node.examples

def GetExtMappingsRDFa(node):
    """Self-contained chunk of RDFa HTML markup with mappings for this term."""
    if (node.isClass()):
        equivs = GetTargets(Unit.GetUnit("owl:equivalentClass"), node)
        if len(equivs) > 0:
            markup = ''
            for c in equivs:
                markup = markup + "<link property=\"owl:equivalentClass\" href=\"%s\"/>\n" % c.id
            return markup
    if (node.isAttribute()):
        equivs = GetTargets(Unit.GetUnit("owl:equivalentProperty"), node)
        if len(equivs) > 0:
            markup = ''
            for c in equivs:
                markup = markup + "<link property=\"owl:equivalentProperty\" href=\"%s\"/>\n" % c.id
            return markup
    return "<!-- no external mappings noted for this term. -->"

def GetJsonLdContext():
    """Generates a basic JSON-LD context file for schema.org."""
    jsonldcontext = "{\n    \"@context\":    {\n"
    jsonldcontext += "        \"@vocab\": \"http://schema.org/\",\n"

    url = Unit.GetUnit("URL")
    date = Unit.GetUnit("Date")
    datetime = Unit.GetUnit("DateTime")

    properties = sorted(GetSources(Unit.GetUnit("typeOf"), Unit.GetUnit("rdf:Property")), key=lambda u: u.id)
    for p in properties:
        range = GetTargets(Unit.GetUnit("rangeIncludes"), p)
        type = None

        if url in range:
            type = "@id"
        elif date in range:
            type = "Date"
        elif datetime in range:
            type = "DateTime"

        if type:
            jsonldcontext += "        \"" + p.id + "\": { \"@type\": \"" + type + "\" },"

    jsonldcontext += "}}\n"
    jsonldcontext = jsonldcontext.replace("},}}","}\n    }\n}")
    jsonldcontext = jsonldcontext.replace("},","},\n")

    return jsonldcontext

PageCache = {}

class ShowUnit (webapp2.RequestHandler):
    """ShowUnit exposes schema.org terms via Web RequestHandler
    (HTML/HTTP etc.).
    """
    usedPrefixes = []
    def emitCacheHeaders(self):
        """Send cache-related headers via HTTP."""
        self.response.headers['Cache-Control'] = "public, max-age=43200" # 12h
        self.response.headers['Vary'] = "Accept, Accept-Encoding"

    def GetCachedText(self, node):
        """Return page text from node.id cache (if found, otherwise None)."""
        global PageCache
        if (node.id in PageCache):
            return PageCache[node.id]
        else:
            return None

    def AddCachedText(self, node, textStrings):
        """Cache text of our page for this node via its node.id.

        We can be passed a text string or an array of text strings.
        """
        global PageCache
        outputText = "".join(textStrings)
        PageCache[node.id] = outputText
        return outputText

    def write(self, str):
        """Write some text to Web server's output stream."""
        self.outputStrings.append(str)

    def GetParentStack(self, node):
        """Returns a hiearchical structured used for site breadcrumbs."""
        thing = Unit.GetUnit("Thing")

        if (node not in self.parentStack):
            self.parentStack.append(node)

        if (Unit.isAttribute(node)):
            self.parentStack.append(Unit.GetUnit("Property"))
            self.parentStack.append(thing)

        sc = Unit.GetUnit("rdfs:subClassOf")
        if GetTargets(sc, node):
            for p in GetTargets(sc, node):
                self.GetParentStack(p)
        else:
            # Enumerations are classes that have no declared subclasses
            sc = Unit.GetUnit("typeOf")
            for p in GetTargets(sc, node):
                self.GetParentStack(p)


#Put 'Thing' to the end for multiple inheritance classes
        if(thing in self.parentStack):
	        self.parentStack.remove(thing)
	        self.parentStack.append(thing)

    def ml(self, node, label='', title='', prop=''):
        """ml ('make link')
        Returns an HTML-formatted link to the class or property URL

        * label = optional anchor text label for the link
        * title = optional title attribute on the link
        * prop = an optional property value to apply to the A element
        """

        if label=='':
          label = self.getIdWithPrefix(node)
        if title != '':
          title = " title=\"%s\"" % (title)
        if prop:
            prop = " property=\"%s\"" % (prop)

        classId = ""
        if(node.prefixes()):
            classId = " class=\"prefix-%s\"" % node.getPrefix()

        return "<a href=\"%s\"%s%s%s>%s</a>" % (node.id, prop, title, classId, label)

    def getIdWithPrefix(self,node):
        if(node.prefixes()):
            prefix = node.getPrefix()
            if(prefix not in self.usedPrefixes):
                self.usedPrefixes.append(prefix)
            return "%s:%s" % (prefix, node.id)
        return node.id


    def makeLinksFromArray(self, nodearray, tooltip=''):
        """Make a comma separate list of links via ml() function.

        * tooltip - optional text to use as title of all links
        """
        hyperlinks = []
        for f in nodearray:
           hyperlinks.append(self.ml(f, f.id, tooltip))
        return (", ".join(hyperlinks))

    def UnitHeaders(self, node):
        """Write out the HTML page headers for this node."""
        self.write("<h1 class=\"page-title\">\n")
        self.write(node.id)
        self.write("</h1>")
        self.BreadCrums(node)
        comment = GetComment(node)
        self.write(" <div property=\"rdfs:comment\">%s</div>\n\n" % (comment) + "\n")
        if (node.isClass() and not node.isDataType()):
            self.write("<table class=\"definition-table\">\n        <thead>\n  <tr><th>Property</th><th>Expected Type</th><th>Description</th>               \n  </tr>\n  </thead>\n\n")

# Stacks to support multiple inheritance
    crumStacks = []
    def BreadCrums(self, node):
        self.crumStacks = []
        cstack = []
        self.crumStacks.append(cstack)
        self.WalkCrums(node,cstack)
        if (Unit.isAttribute(node)):
            cstack.append(Unit.GetUnit("Property"))
            cstack.append(Unit.GetUnit("Thing"))
       
        enuma = node.isEnumerationValue()

        for row in range(len(self.crumStacks)):
           count = 0
           self.write("<h4>")
           self.write("<span class='breadcrums'>")
           while(len(self.crumStacks[row]) > 0):
                if(count > 0):
                    if((len(self.crumStacks[row]) == 1) and enuma):
                        self.write(" :: ")
                    else:
                        self.write(" &gt; ")
                n = self.crumStacks[row].pop()
                self.write("%s" % (self.ml(n)))
                count += 1
           self.write("</span>")
           self.write("</h4>\n")

#Walk up the stack, appending crums & create new (duplicating crums already identified) if more than one parent found
    def WalkCrums(self, node, cstack):
        cstack.append(node)
        tmpStacks = []
        tmpStacks.append(cstack)
        sc = []
        if (Unit.isClass(node)):
            sc = Unit.GetUnit("rdfs:subClassOf")
        elif(Unit.isAttribute(node)):
            sc = Unit.GetUnit("rdfs:subPropertyOf")
        else:
            sc = Unit.GetUnit("typeOf")# Enumerations are classes that have no declared subclasses

        subs = GetTargets(sc, node)
        for i in range(len(subs)):
            if(i > 0):
                t = cstack[:]
                tmpStacks.append(t)
                self.crumStacks.append(t)        
        x = 0
        for p in subs:
            self.WalkCrums(p,tmpStacks[x])
            x += 1

    def ClassProperties (self, cl, subclass=False):
        headerPrinted = False
        di = Unit.GetUnit("domainIncludes")
        ri = Unit.GetUnit("rangeIncludes")
        for prop in sorted(GetSources(di, cl), key=lambda u: u.id):
            if (prop.superceded()):
                continue
            supercedes = prop.supercedes()
            inverseprop = prop.inverseproperty()
            subprops = prop.subproperties()
            superprops = prop.superproperties()
            ranges = GetTargets(ri, prop)
            comment = GetComment(prop)
            if (not headerPrinted):
                class_head = self.ml(cl)
                if subclass:
                    class_head = self.ml(cl, prop="rdfs:subClassOf")
                self.write("<thead class=\"supertype\">\n  <tr>\n    <th class=\"supertype-name\" colspan=\"3\">Properties from %s</th>\n  </tr>\n</thead>\n\n<tbody class=\"supertype\">\n  " % (class_head))
                headerPrinted = True

            self.write("\n<tr typeof=\"rdfs:Property\" resource=\"%s%s\">\n    \n      <th class=\"prop-nam\" scope=\"row\">\n\n<code property=\"rdfs:label\">%s</code>\n    </th>\n " % (Unit.pathForPrefix(prop.prefix),prop.id, self.ml(prop)))
            self.write("<td class=\"prop-ect\">\n")
            first_range = True
            for r in ranges:
                if (not first_range):
                    self.write(" or <br/> ")
                first_range = False
                self.write(self.ml(r, prop='rangeIncludes'))
                self.write("&nbsp;")
            self.write("</td>")
            self.write("<td class=\"prop-desc\" property=\"rdfs:comment\">%s" % (comment))
            if (supercedes != None):
                self.write(" Supercedes %s." % (self.ml(supercedes)))
            if (inverseprop != None):
                self.write("<br/> Inverse property: %s." % (self.ml(inverseprop)))

            self.write("</td></tr>")
            subclass = False

        if subclass: # in case the superclass has no defined attributes
            self.write("<meta property=\"rdfs:subClassOf\" content=\"%s\">" % (cl.id))

    def ClassIncomingProperties (self, cl):
        """Write out a table of incoming properties for a per-type page."""
        headerPrinted = False
        di = Unit.GetUnit("domainIncludes")
        ri = Unit.GetUnit("rangeIncludes")
        for prop in sorted(GetSources(ri, cl), key=lambda u: u.id):
            if (prop.superceded()):
                continue
            supercedes = prop.supercedes()
            inverseprop = prop.inverseproperty()
            subprops = prop.subproperties()
            superprops = prop.superproperties()
            ranges = GetTargets(di, prop)
            comment = GetComment(prop)

            if (not headerPrinted):
                self.write("<br/><br/>Instances of %s may appear as values for the following properties<br/>" % (self.ml(cl)))
                self.write("<table class=\"definition-table\">\n        \n  \n<thead>\n  <tr><th>Property</th><th>On Types</th><th>Description</th>               \n  </tr>\n</thead>\n\n")

                headerPrinted = True

            self.write("<tr>\n<th class=\"prop-nam\" scope=\"row\">\n <code>%s</code>\n</th>\n " % (self.ml(prop)) + "\n")
            self.write("<td class=\"prop-ect\">\n")
            first_range = True
            for r in ranges:
                if (not first_range):
                    self.write(" or<br/> ")
                first_range = False
                self.write(self.ml(r))
                self.write("&nbsp;")
            self.write("</td>")
            self.write("<td class=\"prop-desc\">%s " % (comment))
            if (supercedes != None):
                self.write(" Supercedes %s." % (self.ml(supercedes)))
            if (inverseprop != None):
                self.write("<br/> inverse property: %s." % (self.ml(inverseprop)) )

            self.write("</td></tr>")
        if (headerPrinted):
            self.write("</table>\n")


    def AttributeProperties (self, node):
        """Write out properties of this property, for a per-property page."""
        di = Unit.GetUnit("domainIncludes")
        ri = Unit.GetUnit("rangeIncludes")
        ranges = sorted(GetTargets(ri, node), key=lambda u: u.id)
        domains = sorted(GetTargets(di, node), key=lambda u: u.id)
        first_range = True

        supercedes = node.supercedes()
        inverseprop = node.inverseproperty()
        subprops = node.subproperties()
        superprops = node.superproperties()

        if (inverseprop != None):
            tt = "This means the same thing, but with the relationship direction reversed."
            self.write("<p>Inverse-property: %s.</p>" % (self.ml(inverseprop, inverseprop.id,tt)) )

        self.write("<table class=\"definition-table\">\n")
        self.write("<thead>\n  <tr>\n    <th>Values expected to be one of these types</th>\n  </tr>\n</thead>\n\n  <tr>\n    <td>\n      ")

        for r in ranges:
            if (not first_range):
                self.write("<br/>")
            first_range = False
            tt = "The '%s' property has values that include instances of the '%s' type." % (node.id, r.id)
            self.write(" <code>%s</code> " % (self.ml(r, r.id, tt))+"\n")
        self.write("    </td>\n  </tr>\n</table>\n\n")
        first_domain = True

        self.write("<table class=\"definition-table\">\n")
        self.write("  <thead>\n    <tr>\n      <th>Used on these types</th>\n    </tr>\n</thead>\n<tr>\n  <td>")
        for d in domains:
            if (not first_domain):
                self.write("<br/>")
            first_domain = False
            tt = "The '%s' property is used on the '%s' type." % (node.id, d.id)
            self.write("\n    <code>%s</code> " % (self.ml(d, d.id, tt, prop="domainIncludes"))+"\n")
        self.write("      </td>\n    </tr>\n</table>\n\n")

        if (len(subprops) > 0):
            self.write("<table class=\"definition-table\">\n")
            self.write("  <thead>\n    <tr>\n      <th>Sub-properties</th>\n    </tr>\n</thead>\n")
            for sbp in subprops:
                c = GetComment(sbp)
                tt = "%s: ''%s''" % ( sbp.id, c)
                self.write("\n    <tr><td><code>%s</code></td></tr>\n" % (self.ml(sbp, sbp.id, tt)))
            self.write("\n</table>\n\n")

        if (len(superprops) > 0):
            self.write("<table class=\"definition-table\">\n")
            self.write("  <thead>\n    <tr>\n      <th>Super-properties</th>\n    </tr>\n</thead>\n")
            for spp in superprops:
                c = GetComment(spp)
                tt = "%s: ''%s''" % ( spp.id, c)
                self.write("\n    <tr><td><code>%s</code></td></tr>\n" % (self.ml(spp, spp.id, tt)))
            self.write("\n</table>\n\n")

    def rep(self, markup):
        """Replace < and > with HTML escape chars."""
        m1 = re.sub("<", "&lt;", markup)
        m2 = re.sub(">", "&gt;", m1)
        # TODO: Ampersand? Check usage with examples.
        return m2

    def getHomepage(self, node):
        """Send the homepage, or if no HTML accept header received and JSON-LD was requested, send JSON-LD context file.

        typical browser accept list: ('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8')
        # e.g. curl -H "Accept: application/ld+json" http://localhost:8080/
        see also http://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html
        https://github.com/rvguha/schemaorg/issues/5
        https://github.com/rvguha/schemaorg/wiki/JsonLd
        """
        accept_header = self.request.headers.get('Accept').split(',')
        logging.info("accepts: %s" % self.request.headers.get('Accept'))
        if ENABLE_JSONLD_CONTEXT:
            jsonldcontext = GetJsonLdContext() # consider memcached?

        mimereq = {}
        for ah in accept_header:
            ah = re.sub( r";q=\d?\.\d+", '', ah).rstrip()
            mimereq[ah] = 1

        html_score = mimereq.get('text/html', 5)
        xhtml_score = mimereq.get('application/xhtml+xml', 5)
        jsonld_score = mimereq.get('application/ld+json', 10)
        # print "accept_header: " + str(accept_header) + " mimereq: "+str(mimereq) + "Scores H:{0} XH:{1} J:{2} ".format(html_score,xhtml_score,jsonld_score)

        if (ENABLE_JSONLD_CONTEXT and (jsonld_score < html_score and jsonld_score < xhtml_score)):
            self.response.headers['Content-Type'] = "application/ld+json"
            self.emitCacheHeaders()
            self.response.out.write( jsonldcontext )
            return
        else:
            self.response.out.write( open("static/index.html", 'r').read() )
            return

    def getExactTermPage(self, node):
        """Emit a Web page that exactly matches this node."""
        self.outputStrings = []
        self.usedPrefixes = []

        ext_mappings = GetExtMappingsRDFa(node)

#        headers.OutputSchemaorgHeaders(self, node.id, node.isClass(), ext_mappings)
        extras = ""
        if(Unit.prefixes()):
            extras += " prefix=\""
            for pa, pr in PrefixMap.items():
                extras += "%s:%s "% (pr, pa)
            extras += "\""

        headers.OutputSchemaorgHeaders(self, Unit.pathForPrefix(node.prefix), node.id, node.isClass(), extras, ext_mappings)
        cached = self.GetCachedText(node)
        if (cached != None):
            self.response.write(cached)
            return

        self.parentStack = []
        self.GetParentStack(node)

        self.UnitHeaders(node)

        if (node.isClass()):
            subclass = True
            for p in self.parentStack:
                self.ClassProperties(p, p==self.parentStack[0])
            self.write("</table>\n")
            self.ClassIncomingProperties(node)
        elif (Unit.isAttribute(node)):
            self.AttributeProperties(node)

        if (not Unit.isAttribute(node)):
            self.write("\n\n</table>\n\n") # no supertype table for properties

        if (node.isClass()):
            children = sorted(GetSources(Unit.GetUnit("rdfs:subClassOf"), node), key=lambda u: u.id)
            if (len(children) > 0):
                self.write("<br/><b>More specific Types</b>");
                for c in children:
                    self.write("<li> %s" % (self.ml(c)))

        if (node.isEnumeration()):
            children = sorted(GetSources(Unit.GetUnit("typeOf"), node), key=lambda u: u.id)
            if (len(children) > 0):
                self.write("<br/><br/>Enumeration members");
                for c in children:
                    self.write("<li> %s" % (self.ml(c)))

        if(Unit.prefixes() and len(self.usedPrefixes) > 0):
            self.write("<h4 id='prefixes'>Prefixes used:</h4>\n")
            for pa, pr in PrefixMap.items():
                if(pr in self.usedPrefixes):            
                    self.write("<li>%s: %s</li>\n" % (pr, pa))

        ackorgs = GetTargets(Unit.GetUnit("dc:source"), node)
        if (len(ackorgs) > 0):
            self.write("<h4  id=\"acks\">Acknowledgements</h4>\n")
            for ao in ackorgs:
                acks = sorted(GetTargets(Unit.GetUnit("rdfs:comment"), ao))
                for ack in acks:
                    self.write(str(ack+"<br/>"))
                if(not acks):
                    self.write("Derived from: <a href='%s'>%s</a><br/>" % (ao.id,ao.id))

        examples = GetExamples(node)
        if (len(examples) > 0):
            example_labels = [
              ('Without Markup', 'original_html', 'selected'),
              ('Microdata', 'microdata', ''),
              ('RDFa', 'rdfa', ''),
              ('JSON-LD', 'jsonld', ''),
            ]
            self.write("<h4 class=\"example\">Examples</h4>")
            exinc = 0
            for ex in examples:
                self.write("<ul class=\"nav nav-tabs\">")
                for label, example_type, selected in example_labels:
                    self.write("<li")
                    if selected:
                        self.write(" class=\"active\"")
                    self.write("><a href=\"#"+example_type+"_"+str(exinc)+"\" data-toggle=\"tab\">"+label+"</a>")
                    self.write("</li>")
                self.write("</ul>")
                self.write("<div class=\"tab-content\">\n")
                for label, example_type, selected in example_labels:
                    self.write("<div class=\"tab-pane")
                    if selected:
                        self.write(" active")
                    self.write("\" id=\""+example_type+"_"+str(exinc)+"\">")
                    exmpl = ex.get(example_type)
                    if(len(exmpl) > 0): 
                        self.write("<pre class=\"prettyprint lang-html linenums %s %s\">%s</pre>\n" % (example_type, selected, self.rep(exmpl)))
                    else:
                        txt = "No " + label + " example available."
                        self.write("<pre class=\"prettyprint lang-html linenums %s %s\">%s</pre>\n" % (example_type, selected, self.rep(txt)))
                    self.write("</div>")
                self.write("</div>\n")
                exinc = exinc + 1

#        self.write("<p class=\"version\">")
#        for v in VersionList:
#             self.write("%s<br/>\n" % (v))
#        self.write("Schemaorg Code Version %s</p>\n" % SCHEMA_VERSION)
        self.write(self.getVersion())

        # Analytics
        self.write("""<script>(function(i,s,o,g,r,a,m){i['GoogleAnalyticsObject']=r;i[r]=i[r]||function(){
          (i[r].q=i[r].q||[]).push(arguments)},i[r].l=1*new Date();a=s.createElement(o),
          m=s.getElementsByTagName(o)[0];a.async=1;a.src=g;m.parentNode.insertBefore(a,m)
          })(window,document,'script','//www.google-analytics.com/analytics.js','ga');
          ga('create', 'UA-52860601-1', 'auto');ga('send', 'pageview');</script>""")

#        self.write(" \n\n</div>\n</body>\n</html>")
        self.write(headers.footers)

        self.response.write(self.AddCachedText(node, self.outputStrings))

    @staticmethod
    def getVersion():
        disp = "<p class=\"version\">"
        for v in VersionList:
             disp += "%s<br/>\n" % (v)
        disp += "Schemaorg Code Version %s</p>\n" % SCHEMA_VERSION
        return disp
         
    def get(self, node):
        """Get a schema.org site page generated for this node/term.

        Web content is written directly via self.response.

        CORS enabled all URLs - we assume site entirely public.
        See http://en.wikipedia.org/wiki/Cross-origin_resource_sharing

        These should give a JSON version of schema.org:

            curl --verbose -H "Accept: application/ld+json" http://localhost:8080/docs/jsonldcontext.json
            curl --verbose -H "Accept: application/ld+json" http://localhost:8080/docs/jsonldcontext.json.txt
            curl --verbose -H "Accept: application/ld+json" http://localhost:8080/

        Per-term pages vary for type, property and enumeration.

        Last resort is a 404 error if we do not exactly match a term's id.


        See also https://webapp-improved.appspot.com/guide/request.html#guide-request
        """

        self.response.headers.add_header("Access-Control-Allow-Origin", "*") # entire site is public.

#        TODO: redirections - https://github.com/rvguha/schemaorg/issues/4
#        or https://webapp-improved.appspot.com/guide/routing.html?highlight=redirection
#
#        if str(self.request.host).startswith("www.schema.org"):
#            log.debug("www.schema.org requested. We should redirect to use schema.org as hostname, not " + self.request.host)
#            origURL = self.request.url
#            origURL.replace("https://", "http://")
#            origURL.replace("//www.schema.org", "//schema.org")
#            self.redirect(newURL, permanent=True)

        # First: fixed paths: homepage, favicon.ico and generated JSON-LD files.
        #
        if (node == "" or node=="/"):
            self.getHomepage(node)
            return

        if ENABLE_JSONLD_CONTEXT:
            if (node=="docs/jsonldcontext.json.txt"):
                jsonldcontext = GetJsonLdContext()
                self.response.headers['Content-Type'] = "text/plain"
                self.emitCacheHeaders()
                self.response.out.write( jsonldcontext )
                return
            if (node=="docs/jsonldcontext.json"):
                jsonldcontext = GetJsonLdContext()
                self.response.headers['Content-Type'] = "application/ld+json"
                self.emitCacheHeaders()
                self.response.out.write( jsonldcontext )
                return

        if (node == "favicon.ico"):
            return
        if (node == "robots.txt"):
            robots = "robots.txt"
            if os.path.isfile(robots):
                self.response.out.write( open(robots, 'r').read() )
            return

        # Next: pages based on request path matching a Unit in the term graph.
        node = Unit.GetUnit(node) # e.g. "Person", "CreativeWork".

        # TODO:
        # - handle http vs https; www.schema.org vs schema.org
        # - handle foo-input Action pseudo-properties
        # - handle /Person/Minister -style extensions

        if (node != None):
            self.getExactTermPage(node)
            return
        else:
          self.error(404)
          self.response.out.write('<title>404 Not Found.</title><a href="/">404 Not Found.</a><br/><br/>')
          return

def read_file (filename):
    """Read a file from disk, return it as a single string."""
    import os.path
    folder = os.path.dirname(os.path.realpath(__file__))
    file_path = os.path.join(folder, filename)
    strs = []
    for line in open(file_path, 'r').readlines():
        strs.append(line)
    return "".join(strs)

schemasInitialized = False

def read_schemas():
    """Read/parse/ingest schemas from data/*.rdfa. Also alsodata/*examples.txt"""
    import os.path
    import glob
    global schemasInitialized
    if (not schemasInitialized):
        files = glob.glob("data/*.rdfa")
        schema_contents = []
        for f in files:
            schema_content = read_file(f)
            schema_contents.append(schema_content)

        ft = 'rdfa'
        parser = parsers.MakeParserOfType(ft, None)
        items = parser.parse(schema_contents)
        files = glob.glob("data/*examples.txt")
        example_contents = []
        for f in files:
            example_content = read_file(f)
            example_contents.append(example_content)
        parser = parsers.ParseExampleFile(None)
        parser.parse(example_contents)
        schemasInitialized = True

read_schemas()

app = ndb.toplevel(webapp2.WSGIApplication([("/(.*)", ShowUnit)]))