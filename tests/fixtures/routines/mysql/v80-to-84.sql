-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `replication_report`(IN qualify INT)
BEGIN
  SET @@SESSION.expire_logs_days = 7;
  SELECT WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS('3E11FA47-71CA-11E1-9E33-C80AA9429562:1-5', 10) INTO @waited;
  FLUSH HOSTS;
  SELECT id FROM orders WHERE id > qualify ORDER BY id;
END
