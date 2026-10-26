# Copyright 2026 Gozunga and Coast contributors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Specific errors for collection query parameters."""

from coast.common import exception


EVENT_OUTCOMES = ('start', 'success', 'failure')


def choice(label, value, allowed):
    if value not in allowed:
        raise exception.Invalid(
            "Invalid %s '%s'. Use one of: %s." %
            (label, value, ', '.join(sorted(allowed))))


def parameters(params, allowed):
    for key in sorted(set(params) - set(allowed)):
        raise exception.Invalid(
            "Invalid query parameter '%s' with value '%s'. %s" %
            (key, params[key], 'Use one of: %s.' % ', '.join(sorted(allowed))
             if allowed else 'This endpoint accepts no query parameters.'))


def limit(value, minimum=0):
    try:
        result = int(value)
        if result < minimum:
            raise ValueError()
    except ValueError:
        raise exception.Invalid(
            "Invalid limit '%s'. Use an integer greater than or equal to %s."
            % (value, minimum)) from None
    return result


def filter_value(key, value, requirement):
    return exception.Invalid(
        "Invalid %s filter '%s'. Use %s." % (key, value, requirement))
