# -*- coding: utf-8 -*-
##############################################################################
#
#    printers module for OpenERP, Allow to manage printers un OpenERP
#    Copyright (C) 2011 SYLEAM Info Services (<http://www.Syleam.fr/>)
#              Sylvain Garancher <sylvain.garancher@syleam.fr>
#              Christophe CHAUVET <christophe.chauvet@syleam.fr>
#
#    This file is a part of printers
#
#    printers is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    printers is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp.osv import osv
from openerp.osv import fields
from tools.translate import _
from openerp.modules import get_module_path
from datetime import datetime
from tempfile import mkstemp
import unicodedata
import logging
import netsvc
import os
import cups

from reportlab.pdfgen import canvas
import time

logger = logging.getLogger('printers')


def convert(name):
    """Convert data with no accent and upper mode"""
    return unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').replace('&', '').replace('_', '')


class printers_server(osv.Model):

    """
    Manages printing servers
    """
    _name = 'printers.server'
    _description = 'List of printing servers'
    _order = 'server'
    _rec_name = 'server'

    _columns = {
        'server': fields.char('Server', size=64, required=True, help='Name of the server'),
        'address': fields.char('Address', size=15, required=True, help='IP address or hostname of the server'),
        'port': fields.integer('Port', help='Port of the server'),
        'user': fields.char('User', size=32, help='User to log in on the server'),
        'password': fields.char('Password', size=32, help='Password to log in on the server'),
        'active': fields.boolean('Active', help='If checked, this server is useable'),
        'printer_ids': fields.one2many('printers.list', 'server_id', 'Printers List', help='List of printers available on this server'),
        'custom_user': fields.boolean('Custom User', help='Check this, if you want to use OpenERP User Name, instead of a specific user'),
    }

    _defaults = {
        'address': '127.0.0.1',
        'active': True,
        'port': 0,
        'custom_user': False,
    }

    def _openConnection(self, cr, uid, server_id, context=None):
        server = self.browse(cr, uid, server_id, context=context)
        kwargs = {'host': server.address}
        if server.port:
            kwargs['port'] = int(server.port)
        return cups.Connection(**kwargs)

    def update_printers(self, cr, uid, ids, context=None):
        for server in self.browse(cr, uid, ids, context=context):
            try:
                connection = self._openConnection(cr, uid, server.id, context=context)
            except:
                logger.warning('Update cups printers : Failed to connect to cups server %s (%s:%s)' % (server.server, server.address, server.port))
                continue

            # Update Printers
            printers = connection.getPrinters()
            existing_printers = dict([(printer.code, printer.id) for printer in server.printer_ids])
            for name, printer_info in printers.iteritems():
                printer_values = {
                    'name': printer_info['printer-info'],
                    'code': name,
                    'server_id': server.id,
                    'uri': printer_info['printer-uri-supported'],
                    'printer_state': str(printer_info['printer-state']),
                    'printer_state_message': printer_info['printer-state-message'],
                }
                if name not in existing_printers.keys():
                    self.pool.get('printers.list').create(cr, uid, printer_values, context=context)
                else:
                    self.pool.get('printers.list').write(cr, uid, existing_printers[name], printer_values, context=context)

        return True

    def update_jobs(self, cr, uid, ids=None, context=None, which='all', first_job_id=-1):
        if context is None:
            context = {}

        job_obj = self.pool.get('printers.job')
        printer_obj = self.pool.get('printers.list')

        if ids is None:
            ids = self.search(cr, uid, [], context=context)

        # Update printers list, in order to ensure that jobs printers will be in OpenERP
        self.update_printers(cr, uid, ids, context=context)

        for server in self.browse(cr, uid, ids, context=context):
            try:
                connection = self._openConnection(cr, uid, server.id, context=context)
            except:
                logger.warning('Update cups jobs : Failed to connect to cups server %s (%s:%s)' % (server.server, server.address, server.port))
                continue

            # Retrieve asked job data
            jobs_data = connection.getJobs(which_jobs=which, first_job_id=first_job_id, requested_attributes=[
                'job-name',
                'job-id',
                'printer-uri',
                'job-media-progress',
                'time-at-creation',
                'job-state',
                'job-state-reasons',
                'time-at-processing',
                'time-at-completed',
            ])

            # Retrieve known uncompleted jobs data to update them
            if which == 'not-completed':
                min_job_ids = job_obj.search(cr, uid, [('job_state', 'not in', ('7', '8', '9')), ('active', '=', True)], limit=1, order='jobid', context=context)
                if min_job_ids:
                    min_job_id = job_obj.browse(cr, uid, min_job_ids[0], context=context).jobid
                    jobs_data.update(connection.getJobs(which_jobs='completed', first_job_id=min_job_id, requested_attributes=[
                        'job-name',
                        'job-id',
                        'printer-uri',
                        'job-media-progress',
                        'time-at-creation',
                        'job-state',
                        'job-state-reasons',
                        'time-at-processing',
                        'time-at-completed',
                    ]))

            all_cups_job_ids = set()
            for cups_job_id, job_data in jobs_data.items():
                all_cups_job_ids.add(cups_job_id)
                job_ids = job_obj.search(cr, uid, [('jobid', '=', cups_job_id), ('server_id', '=', server.id)], context=dict(context, active_test=False))
                job_values = {
                    'name': job_data.get('job-name', ''),
                    'active': True,
                    'server_id': server.id,
                    'jobid': cups_job_id,
                    'job_media_progress': job_data.get('job-media-progress', 0),
                    'time_at_creation': job_data.get('time-at-creation', ''),
                    'job_state': str(job_data.get('job-state', '')),
                    'job_state_reason': job_data.get('job-state-reasons', ''),
                    'time_at_creation': datetime.fromtimestamp(job_data.get('time-at-creation', 0)).strftime('%Y-%m-%d %H:%M:%S'),
                    'time_at_processing': job_data.get('time-at-processing', 0) and datetime.fromtimestamp(job_data.get('time-at-processing', 0)).strftime('%Y-%m-%d %H:%M:%S'),
                    'time_at_completed': job_data.get('time-at-completed', 0) and datetime.fromtimestamp(job_data.get('time-at-completed', 0)).strftime('%Y-%m-%d %H:%M:%S'),
                }

                # Search for the printer in OpenERP
                printer_uri = job_data['printer-uri']
                printer_code = printer_uri[printer_uri.rfind('/') + 1:]
                printer_id = printer_obj.search(cr, uid, [('server_id', '=', server.id), ('code', '=', printer_code)], context=context)
                job_values['printer_id'] = printer_id[0]

                if job_ids:
                    job_obj.write(cr, uid, job_ids, job_values, context=context)
                else:
                    job_obj.create(cr, uid, job_values, context=context)

            # Deactive purged jobs
            if which == 'all' and first_job_id == -1:
                purged_job_ids = job_obj.search(cr, uid, [('jobid', 'not in', list(all_cups_job_ids))], context=context)
                job_obj.write(cr, uid, purged_job_ids, {'active': False}, context=context)

        return True


class printers_manufacturer(osv.Model):

    """
    Manage printer per manufacturer
    """
    _name = 'printers.manufacturer'
    _description = 'Printer manufacturer'
    _order = 'name'

    _columns = {
        'name': fields.char('Name', size=32, required=True, help='Name of this manufacturer'),
        'code': fields.char('Code', size=16, help='Code of this manufacturer'),
        'website': fields.char('Website', size=128, help='Website address of this manufacturer'),
    }


class printers_type(osv.Model):

    """
    Printer per type
    """
    _name = 'printers.type'
    _description = 'List of printers types'
    _order = 'name'

    _columns = {
        'name': fields.char('Name', size=32, required=True, translate=True, help='Name of this type'),
        'description': fields.char('Description', size=64, help='Description for this type'),
        'printer_ids': fields.one2many('printers.list', 'type_id', 'Printers'),
    }


class PrintersEncoding(osv.Model):
    _name = 'printers.encoding'
    _description = 'Printer Encoding'

    _columns = {
        'name': fields.char('Name', size=64, required=True, help='Name of the encoding'),
        'code': fields.char('Code', size=16, help='Encoding code'),
    }


class printers_list(osv.Model):

    """
    Manage printers
    """
    _name = 'printers.list'
    _description = 'List of printers per server'
    _order = 'name'

    def _get_default_encoding(self, cr, uid, context=None):
        """
        Returns a default encoding (first found)
        Nothing if no encoding is in the database
        """
        encoding_ids = self.pool.get('printers.encoding').search(cr, uid, [], limit=1, context=context)
        return encoding_ids and encoding_ids[0] or False

    _columns = {
        'name': fields.char('Printer Name', size=64, required=True, help='Printer\'s name'),
        'code': fields.char('Printer Code', size=64, required=True, help='Printer\'s code'),
        'server_id': fields.many2one('printers.server', 'Server', required=True, help='Printer server'),
        'type_id': fields.many2one('printers.type', 'Type', help='Printer type'),
        'active': fields.boolean('Active', help='If checked, this link  printer/server is active'),
        'manufacturer_id': fields.many2one('printers.manufacturer', 'Manufacturer', help='Printer\'s manufacturer'),
        'fitplot': fields.boolean('Fitplot', help='If checked, scales the print file to fit on the page'),
        'uri': fields.char('URI', size=256, required=True, help='URI of this printer'),
        'printer_state': fields.selection([
            ('3', 'Idle'),
            ('4', 'Printing'),
            ('5', 'Stopped'),
        ], 'State', readonly=True, help='Current state of the printer'),
        'printer_state_message': fields.char('State Message', size=256, readonly=True, help='Messages associated to the current state of the printer'),
        'encoding_id': fields.many2one('printers.encoding', 'Encoding', required=True, help='Encoding used by this printer'),
    }

    _defaults = {
        'active': True,
        'fitplot': False,
        'encoding_id': _get_default_encoding,
    }

    def _command(self, cr, uid, printer_id, print_type, print_data, context=None):
        """
        Print a file on the selected CUPS server
        TODO : When available from pycups to print stdin data, rewrite the temp file part
        """
        if context is None:
            context = {}

        server_obj = self.pool.get('printers.server')

        # Retrieve printer
        printer = self.browse(cr, uid, printer_id, context=context)

        try:
            connection = server_obj._openConnection(cr, uid, printer.server_id.id, context=context)
        except:
            raise osv.except_osv(_('Error'), _('Connection to the CUPS server failed\nCups server : %s (%s:%s)') % (printer.server_id.server, printer.server_id.address, printer.server_id.port))

        # Define printing options
        options = {}

        # Add the fitplot option
        if printer.fitplot:
            options['fitplot'] = 'fitplot'

        filename = None
        delete_file = False
        if print_type == 'report':
            # Retrieve data to generate the report
            report_data = self.pool.get('ir.actions.report.xml').read(cr, uid, print_data['report_id'], ['model', 'report_name'], context=context)
            report_service = netsvc.LocalService('report.' + report_data['report_name'])
            datas = {'ids': print_data['print_ids'], 'model': report_data['model']}

            # Log the command to send
            logger.info('Object to print : %s (%s)' % (datas['model'], repr(datas['ids'][0])))
            logger.info('Report to print : %s (%s)' % (report_data['report_name'], print_data['report_id']))

            # The commit is necessary for Jasper find the data in PostgreSQL
            cr.commit()

            # Generate the file to print
            (data, format) = report_service.create(cr, uid, print_data['print_ids'], datas, context=context)
            fd, filename = mkstemp(suffix='.' + format, prefix='printers-')
            os.write(fd, data)
            os.close(fd)
            delete_file = True
        elif print_type == 'file':
            filename = print_data['filename']
        elif print_type == 'raw':
            # Define the raw option for cups
            options['raw'] = 'raw'

            # Write the data into a file
            fd, filename = mkstemp(suffix='.raw', prefix='printers-')
            os.write(fd, print_data)
            os.close(fd)
            delete_file = True
        else:
            raise osv.except_osv(_('Error'), _('Unknown command type, unable to print !'))

        # TODO : Rewrite using the cupsCreateJob/cupsStartDocument/cupsWriteRequestData/cupsFinishDocument functions, when available in pycups, instead of writing data into a temporary file
        jobid = False
        try:
            jobid = connection.printFile(printer.code, filename, context.get('jobname', 'OpenERP'), options)
        finally:
            # Remove the file and free the memory
            if delete_file:
                os.remove(filename)

        # Operation successful, return True
        logger.info('Printers Job ID : %d' % jobid)
        server_obj.update_jobs(cr, uid, ids=[printer.server_id.id], context=context, which='all', first_job_id=jobid)
        return jobid

    def send_printer(self, cr, uid, printer_id, report_id, print_ids, context=None):
        """
        Sends a report to a printer
        """
        return self._command(cr, uid, printer_id, 'report', {'report_id': report_id, 'print_ids': print_ids}, context=context)

    def print_file(self, cr, uid, printer_id, filename, context=None):
        """
        Sends a file to a printer
        """
        if context is None:
            context = {}

        ctx = context.copy()
        if filename and not context.get('jobname'):
            ctx['jobname'] = filename.split('/')[-1]
        return self._command(cr, uid, printer_id, 'file', {'filename': filename}, context=ctx)

    def print_raw_data(self, cr, uid, printer_id, data, context=None):
        """
        Sends a file to a printer
        """
        return self._command(cr, uid, printer_id, 'raw', data, context=context)

    def print_test(self, cr, uid, ids, context=None):
        """
        Compose a PDF with printer information, and send it to the printer
        """
        if context is None:
            context = {}

        for printer in self.browse(cr, uid, ids, context=context):
            ctx = context.copy()
            filename = "/tmp/test-printer-openerp-%d.pdf" % printer.id
            ctx['jobname'] = 'OpenERP Test Page (id: %d)' % printer.id

            c = canvas.Canvas(filename)
            c.drawString(100, 805, "Welcome to OpenERP printers module")
            c.drawString(100, 765, "Printer: %s" % printer.name)
            c.line(138, 760, 400, 760)
            c.drawString(100, 740, "Serveur: %s" % printer.server_id.server)
            c.line(145, 735, 400, 735)
            c.drawString(480, 805, time.strftime('%Y-%m-%d'))

            # Draw Rectangle
            c.line(20, 20, 570, 20)
            c.line(20, 820, 570, 820)
            c.line(20, 20, 20, 820)
            c.line(570, 820, 570, 20)

            # Titre en Haut
            c.line(20, 800, 570, 800)
            c.line(450, 800, 450, 820)

            # Add logo
            c.drawImage(os.path.join(get_module_path('printers'), 'static', 'src', 'img', 'logo.jpg'), 25, 730, 64, 64)
            c.save()

            # Send this file to the printer
            self.print_file(cr, uid, printer.id, filename, context=ctx)

        return True

    def cancelAll(self, cr, uid, ids, context=None, purge_jobs=False):
        server_obj = self.pool.get('printers.server')
        for printer in self.browse(cr, uid, ids, context=context):
            connection = server_obj._openConnection(cr, uid, printer.server_id.id, context=context)
            connection.cancelAllJobs(name=printer.code, purge_jobs=purge_jobs)
            server_obj.update_jobs(cr, uid, ids=[printer.server_id.id], context=context, which='completed')

        return True

    def enable(self, cr, uid, ids, context=None):
        server_obj = self.pool.get('printers.server')
        for printer in self.browse(cr, uid, ids, context=context):
            connection = server_obj._openConnection(cr, uid, printer.server_id.id, context=context)
            connection.enablePrinter(printer.code)
            server_obj.update_printers(cr, uid, ids=[printer.server_id.id], context=context)

        return True

    def disable(self, cr, uid, ids, context=None):
        server_obj = self.pool.get('printers.server')
        for printer in self.browse(cr, uid, ids, context=context):
            connection = server_obj._openConnection(cr, uid, printer.server_id.id, context=context)
            connection.disablePrinter(printer.code)
            server_obj.update_printers(cr, uid, ids=[printer.server_id.id], context=context)

        return True


class printers_label(osv.Model):

    """
    Label board
    """
    _name = 'printers.label'
    _description = 'Label board'

    _columns = {
        'type_id': fields.many2one('printers.type', 'Printer Type', required=True, help='Type of printer'),
        'name': fields.char('Name', size=64, required=True, help='Name of the label'),
        'width': fields.integer('Width', help='Width of the label, in millimeters'),
        'height': fields.integer('Height', help='Height of the label, in millimeters'),
    }


class printers_language(osv.Model):

    """
    Language support per printer
    """
    _name = 'printers.language'
    _description = 'Printer language'

    _columns = {
        'name': fields.char('Name', size=32, required=True, translate=True, help='Name of the language'),
        'code': fields.char('Code', size=16, required=True, help='Code of the language'),
    }


class printers_job(osv.Model):
    _name = 'printers.job'
    _description = 'Printing Job'
    _order = 'jobid DESC'

    _columns = {
        'name': fields.char('Name', size=64, help='Job name'),
        'active': fields.boolean('Active', help='Unchecked if the job is purged from cups'),
        'jobid': fields.integer('Job ID', required=True, help='CUPS id for this job'),
        'server_id': fields.many2one('printers.server', 'Server', required=True, help='Server which host this job'),
        'printer_id': fields.many2one('printers.list', 'Printer', required=True, help='Printer used for this job'),
        'job_media_progress': fields.integer('Media Progress', required=True, help='Percentage of progress for this job'),
        'time_at_creation': fields.datetime('Time At Creation', required=True, help='Date and time of creation for this job'),
        'time_at_processing': fields.datetime('Time At Processing', help='Date and time of process for this job'),
        'time_at_completed': fields.datetime('Time At Completed', help='Date and time of completion for this job'),
        'job_state': fields.selection([
            ('3', 'Pending'),
            ('4', 'Pending Held'),
            ('5', 'Processing'),
            ('6', 'Processing Stopped'),
            ('7', 'Canceled'),
            ('8', 'Aborted'),
            ('9', 'Completed'),
        ], 'State', help='Current state of the job'),
        'job_state_reason': fields.selection([
            ('none', 'No reason'),
            ('aborted-by-system', 'Aborted by the system'),
            ('compression-error', 'Error in the compressed data'),
            ('document-access-error', 'The URI cannot be accessed'),
            ('document-format-error', 'Error in the document'),
            ('job-canceled-at-device', 'Cancelled at the device'),
            ('job-canceled-by-operator', 'Cancelled by the printer operator'),
            ('job-canceled-by-user', 'Cancelled by the user'),
            ('job-completed-successfully', 'Completed successfully'),
            ('job-completed-with-errors', 'Completed with some errors'),
            ('job-completed(with-warnings', 'Completed with some warnings'),
            ('job-data-insufficient', 'No data has been received'),
            ('job-hold-until-specified', 'Currently held'),
            ('job-incoming', 'Files are currently being received'),
            ('job-interpreting', 'Currently being interpreted'),
            ('job-outgoing', 'Currently being sent to the printer'),
            ('job-printing', 'Currently printing'),
            ('job-queued', 'Queued for printing'),
            ('job-queued-for-marker', 'Printer needs ink/marker/toner'),
            ('job-restartable', 'Can be restarted'),
            ('job-transforming', 'Being transformed into a different format'),
            ('printer-stopped', 'Printer is stopped'),
            ('printer-stopped-partly', 'Printer state reason set to \'stopped-partly\''),
            ('processing-to-stop-point', 'Cancelled, but printing already processed pages'),
            ('queued-in-device', 'Queued at the output device'),
            ('resources-are-not-ready', 'Resources not available to print the job'),
            ('service-off-line', 'Held because the printer is offline'),
            ('submission-interrupted', 'Files were not received in full'),
            ('unsupported-compression', 'Compressed using an unknown algorithm'),
            ('unsupported-document-format', 'Unsupported format'),
        ], 'State Reason', help='Reason for the current job state'),
    }

    _defaults = {
        'active': True,
    }

    _sql_constraints = [
        ('jobid_unique', 'UNIQUE(jobid, server_id)', 'The jobid of the printers job must be unique per server !'),
    ]

    def cancel(self, cr, uid, ids, context=None, purge_job=False):
        server_obj = self.pool.get('printers.server')
        for job in self.browse(cr, uid, ids, context=context):
            connection = server_obj._openConnection(cr, uid, job.server_id.id, context=context)
            connection.cancelJob(job.jobid, purge_job=purge_job)
            server_obj.update_jobs(cr, uid, ids=[job.server_id.id], context=context, which='all', first_job_id=job.jobid)

        return True

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
