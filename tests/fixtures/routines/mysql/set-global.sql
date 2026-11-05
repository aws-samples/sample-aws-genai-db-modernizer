-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `tune_server`()
BEGIN
  SET @@GLOBAL.max_connections = 500, SESSION wait_timeout = 60;
  SET PERSIST innodb_lock_wait_timeout = 10;
END
