-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `quiet_cleanup`()
BEGIN
  SET sql_log_bin = 0;
  DELETE FROM sessions WHERE expires_at < NOW();
  SET @@session.sql_log_bin = 1;
END
