#!/usr/bin/env python
#

import webapp2
import re
from google.appengine.ext import db
from google.appengine.ext import blobstore
from google.appengine.ext.webapp import blobstore_handlers
import xml.etree.ElementTree as ET
import logging
import api

def MakeParserOfType (format, webapp):
    if (format == 'mcf') :
        return MCFParser(webapp)
    elif (format == 'rdfa') :
        return RDFAParser(webapp)
    else :
        return 0

class ParseExampleFile :

    def __init__ (self, webapp):
        self.webapp = webapp
        self.initFields()

    def initFields(self):
        self.currentStr = []
        self.terms = []
        self.egmeta = {}
        self.preMarkupStr = ""
        self.microdataStr = ""
        self.rdfaStr = ""
        self.jsonStr = ""
        self.state= ""

    def nextPart(self, next):
        if (self.state == 'PRE-MARKUP:'):
            self.preMarkupStr = "".join(self.currentStr)
        elif (self.state ==  'MICRODATA:'):
            self.microdataStr = "".join(self.currentStr)
        elif (self.state == 'RDFA:'):
            self.rdfaStr = "".join(self.currentStr)
        elif (self.state == 'JSON:'):
            self.jsonStr = "".join(self.currentStr)
        self.state = next
        self.currentStr = []

    def process_example_id(self, m):
        self.egmeta["id"] = m.group(1)
        logging.debug("Storing ID: %s" % self.egmeta["id"] )
        return ''

    def parse (self, contents):
        content = ""
        egid = re.compile("""#(\S+)\s+""")
        for i in range(len(contents)):
            content += contents[i]

        lines = re.split('\n|\r', content)
        for line in lines:
            # Per-example sections begin with e.g.: 'TYPES: #music-2 Person, MusicComposition, Organization'

            if ((len(line) > 6) and line[:6] == "TYPES:"):
                self.nextPart('TYPES:')
                api.Example.AddExample(self.terms, self.preMarkupStr, self.microdataStr, self.rdfaStr, self.jsonStr, self.egmeta)
                # logging.info("AddExample called with terms %s " % self.terms)
                self.initFields()
                typelist = re.split(':', line)
                self.terms = []
                self.egmeta = {}
                # logging.info("TYPE INFO: '%s' " % line );
                tdata = egid.sub(self.process_example_id, typelist[1]) # strips IDs, records them in egmeta["id"]
                ttl = tdata.split(',')
                for ttli in ttl:
                    ttli = re.sub(' ', '', ttli)
                    # logging.info("TTLI: %s " % ttli); # danbri tmp
                    self.terms.append(api.Unit.GetUnit(ttli, True))
            else:
                tokens = ["PRE-MARKUP:", "MICRODATA:", "RDFA:", "JSON:"]
                for tk in tokens:
                    ltk = len(tk)
                    if (len(line) > ltk-1 and line[:ltk] == tk):
                        self.nextPart(tk)
                        line = line[ltk:]
                if (len(line) > 0):
                    self.currentStr.append(line + "\n")
        api.Example.AddExample(self.terms, self.preMarkupStr, self.microdataStr, self.rdfaStr, self.jsonStr, self.egmeta) # should flush on each block of examples
        # logging.info("Final AddExample called with terms %s " % self.terms)




class RDFAParser :
    def __init__ (self, webapp):
        self.webapp = webapp

    def parse (self, files):
        self.items = {}
        root = []
        for i in range(len(files)):
            logging.info("file: %s" % files[i])
            parser = ET.XMLParser(encoding="utf-8")
            tree = ET.parse(files[i], parser=parser)
            root.append(tree.getroot())

            pre = root[i].findall(".//*[@prefix]")
            for e in range(len(pre)):
                api.Unit.storePrefix(pre[e].get('prefix'))

        for i in range(len(root)):
              self.extractTriples(root[i], None)

        return self.items.keys()

    def stripID (self, str) :
        for k in (api.PrefixMap.keys()):
           if(str.startswith(k)):
              return str[len(k):]
        return str
        
    def extractTriples(self, elem, currentNode):
        typeof = elem.get('typeof')
        resource = elem.get('resource')
        href = elem.get('href')
        property = elem.get('property')
        text = elem.text
        if (property != None):
            if(property == "schema:softwareVersion"):
               api.Unit.storeVersion(text)
            else:
                property = api.Unit.GetUnit(self.stripID(property), True)
                if (href != None) :
                    hrefPrefix = href
                    href = api.Unit.GetUnit(self.stripID(href), True)
               #     self.webapp.write("<br>%s %s %s" % (currentNode, property, href))
                    api.Triple.AddTriple(currentNode, property, href)
                    href.setPrefix(hrefPrefix)
                    self.items[currentNode] = 1
                elif (text != None):
                 #   logging.info("<br>%s %s '%s'" % (currentNode, property, text))
                    api.Triple.AddTripleText(currentNode, property, text)
                    self.items[currentNode] = 1
        if (resource != None):
            currentNode = api.Unit.GetUnit(self.stripID(resource), True)
            if (typeof != None):
                api.Triple.AddTriple(currentNode, api.Unit.GetUnit("typeOf", True), api.Unit.GetUnit(self.stripID(typeof), True))
                currentNode.setPrefix(resource)
        for child in elem.findall('*'):
            self.extractTriples(child,  currentNode)



class MCFParser:


    def __init__ (self, webapp):
        self.webapp = webapp

    def extractUnitName (self, line):
        parts = re.split(':', line)
        name = parts[1]
        return re.sub(' ', '', name)

    def extractPredicateName (self, line):
        parts = re.split(':', line)
        return parts[0]

    def cleanValue (self, value) :
        if (value.find('"') > -1):
            parts = re.split('"', value)
            return parts[1]
        else:
            return re.sub(' ', '', value)

    def extractValues (self, line):
        parts = re.split(':', line)
        raw_values = re.split(',', parts[1])
        values = []
        for rv in raw_values:
            values.append(self.cleanValue(rv))
        return values

    def parse (self, content):
        self.items = {}
        lines = re.split('\n|\r', content)
        unit = None
        for l in lines:
            #   self.webapp.write("Got line" + l)
            if (len(l) > 5 and (l[:5] == "Unit:")) :
                unit = api.Unit.GetUnit(self.extractUnitName(l), True)
                self.items[unit] = 1
                #  self.webapp.write("Got unit:" + unit)
            elif (len(l) > 1 and (l.find(':') > 1)) :
                predicate = apiUnit.GetUnit(self.extractPredicateName(l), True)
                values = self.extractValues(l)
                #   self.webapp.write("<br>Got predicate " + predicate)
                for v in values:
                    api.Triple.AddTriple(unit, predicate, api.Unit.GetUnit(v, True))
        return self.items.keys()
