#
# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.
#

Stage - Post Install USL:
  cmd.run:
    - name: __slot__:salt:setup_conf.conf_cmd('/opt/seagate/cortx/csm/conf/setup.yaml', 'usl:post_install')
    - failhard: True

Stage - Prepare USL:
  cmd.run:
    - name: __slot__:salt:setup_conf.conf_cmd('/opt/seagate/cortx/csm/conf/setup.yaml', 'usl:prepare')
    - failhard: True

{% if "primary" in pillar["cluster"][grains["id"]]["roles"] -%}

Generate TLS certs:
  module.run:
    - csm.generate_csm_tls: []

{% else %}

Copy TLS certs to minions:
  file.recurse:
    - source: salt://components/csm/files/tls/
    - name: /var/csm/tls
    - user: {{ pillar["cortx"]["software"]["csm"]["user"] }}
    - group: {{ pillar["cortx"]["software"]["csm"]["user"] }}
    - file_mode: 600

{% endif %}

