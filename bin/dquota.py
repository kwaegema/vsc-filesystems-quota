#!/usr/bin/env python
#
# Copyright 2012-2015 Ghent University
#
# This file is part of vsc-filesystems-quota,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://vscentrum.be/nl/en),
# the Hercules foundation (http://www.herculesstichting.be/in_English)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.ugent.be/hpcugent/vsc-filesystems-quota
#
# vsc-filesystems-quota is free software: you can redistribute it and/or modify
# it under the terms of the GNU Library General Public License as
# published by the Free Software Foundation, either version 2 of
# the License, or (at your option) any later version.
#
# vsc-filesystems-quota is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU Library General Public License
# along with vsc-filesystems-quota. If not, see <http://www.gnu.org/licenses/>.
#
"""
Script to check for quota transgressions and notify the offending users.

- relies on mmrepquota to get a quick estimate of user quota
- checks all storage systems that are listed in /etc/quota_check.conf
- writes quota information in gzipped json files in the target directory for the
  affected entity (user, project, vo)
- mails a user, vo or project moderator (TODO)

@author Andy Georges
"""
import sys


from vsc.accountpage.client import AccountpageClient
from vsc.config.base import VscStorage
from vsc.filesystem.gpfs import GpfsOperations
from vsc.filesystem.quota.tools import get_mmrepquota_maps, map_uids_to_names
from vsc.filesystem.quota.tools import process_user_quota, process_fileset_quota
from vsc.filesystem.quota.tools import notify_exceeding_users, notify_exceeding_filesets
from vsc.ldap.configuration import VscConfiguration
from vsc.ldap.utils import LdapQuery
from vsc.utils import fancylogger
from vsc.utils.nagios import NAGIOS_EXIT_CRITICAL
from vsc.utils.script_tools import ExtendedSimpleOption

# Constants
NAGIOS_CHECK_INTERVAL_THRESHOLD = 60 * 60  # one hour

# log setup
logger = fancylogger.getLogger(__name__)
fancylogger.logToScreen(True)
fancylogger.setLogLevelInfo()

QUOTA_USERS_WARNING = 20
QUOTA_USERS_CRITICAL = 40
QUOTA_FILESETS_CRITICAL = 1


def main():
    """Main script"""

    options = {
        'nagios-check-interval-threshold': NAGIOS_CHECK_INTERVAL_THRESHOLD,
        'storage': ('the VSC filesystems that are checked by this script', None, 'extend', []),
        'account_page_url': ('Base URL of the account page', None, 'store', 'https://account.vscentrum.be/django'),
        'access_token': ('OAuth2 token to access the account page REST API', None, 'store', None),
    }
    opts = ExtendedSimpleOption(options)

    try:
        client = AccountpageClient(token=opts.options.access_token)

        user_id_map = map_uids_to_names()  # is this really necessary?
        LdapQuery(VscConfiguration())
        gpfs = GpfsOperations()
        storage = VscStorage()

        target_filesystems = [storage[s].filesystem for s in opts.options.storage]

        filesystems = gpfs.list_filesystems(target_filesystems).keys()
        logger.debug("Found the following GPFS filesystems: %s" % (filesystems))

        filesets = gpfs.list_filesets()
        logger.debug("Found the following GPFS filesets: %s" % (filesets))

        quota = gpfs.list_quota()
        exceeding_filesets = {}
        exceeding_users = {}
        stats = {}

        for storage_name in opts.options.storage:

            logger.info("Processing quota for storage_name %s" % (storage_name))
            filesystem = storage[storage_name].filesystem
            replication_factor = storage[storage_name].data_replication_factor

            if filesystem not in filesystems:
                logger.error("Non-existent filesystem %s" % (filesystem))
                continue

            if filesystem not in quota.keys():
                logger.error("No quota defined for storage_name %s [%s]" % (storage_name, filesystem))
                continue

            quota_storage_map = get_mmrepquota_maps(
                quota[filesystem],
                storage_name,
                filesystem,
                filesets,
                replication_factor
            )

            exceeding_filesets[storage_name] = process_fileset_quota(storage,
                                                                     gpfs,
                                                                     storage_name,
                                                                     filesystem,
                                                                     quota_storage_map['FILESET'],
                                                                     client,
                                                                     opts.options.dry_run)
            exceeding_users[storage_name] = process_user_quota(storage,
                                                               gpfs,
                                                               storage_name,
                                                               filesystem,
                                                               quota_storage_map['USR'],
                                                               user_id_map,
                                                               client,
                                                               opts.options.dry_run)

            stats["%s_fileset_critical" % (storage_name,)] = QUOTA_FILESETS_CRITICAL
            if exceeding_filesets[storage_name]:
                stats["%s_fileset" % (storage_name,)] = 1
                logger.warning("storage_name %s found %d filesets that are exceeding their quota",
                               storage_name, len(exceeding_filesets))
                for (e_fileset, e_quota) in exceeding_filesets[storage_name]:
                    logger.warning("%s has quota %s" % (e_fileset, str(e_quota)))
            else:
                stats["%s_fileset" % (storage_name,)] = 0
                logger.debug("storage_name %s found no filesets that are exceeding their quota" % storage_name)

            notify_exceeding_filesets(gpfs=gpfs,
                                      storage=storage_name,
                                      filesystem=filesystem,
                                      exceeding_items=exceeding_filesets[storage_name],
                                      client=client,
                                      dry_run=opts.options.dry_run)

            stats["%s_users_warning" % (storage_name,)] = QUOTA_USERS_WARNING
            stats["%s_users_critical" % (storage_name,)] = QUOTA_USERS_CRITICAL
            if exceeding_users[storage_name]:
                stats["%s_users" % (storage_name,)] = len(exceeding_users[storage_name])
                logger.warning("storage_name %s found %d users who are exceeding their quota" %
                               (storage_name, len(exceeding_users[storage_name])))
                for (e_user_id, e_quota) in exceeding_users[storage_name]:
                    logger.warning("%s has quota %s" % (e_user_id, str(e_quota)))
            else:
                stats["%s_users" % (storage_name,)] = 0
                logger.debug("storage_name %s found no users who are exceeding their quota" % storage_name)

            notify_exceeding_users(gpfs=gpfs,
                                   storage=storage_name,
                                   filesystem=filesystem,
                                   exceeding_items=exceeding_users[storage_name],
                                   client=client,
                                   dry_run=opts.options.dry_run)
    except Exception, err:
        logger.exception("critical exception caught: %s" % (err))
        opts.critical("Script failed in a horrible way")
        sys.exit(NAGIOS_EXIT_CRITICAL)

    opts.epilogue("quota check completed", stats)

if __name__ == '__main__':
    main()
