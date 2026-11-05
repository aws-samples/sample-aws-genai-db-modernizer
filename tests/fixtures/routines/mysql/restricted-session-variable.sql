-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `row_logging`()
BEGIN
  SET SESSION binlog_format = 'ROW';
  UPDATE orders SET total = total WHERE id = 0;
END
