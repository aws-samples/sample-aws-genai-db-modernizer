-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `enable_audit`()
BEGIN
  INSTALL PLUGIN audit_log SONAME 'audit_log.so';
END
