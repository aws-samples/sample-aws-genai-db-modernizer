-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `undo_handler`()
BEGIN
  DECLARE UNDO HANDLER FOR SQLEXCEPTION SET @failed = 1;
  DELETE FROM orders WHERE id = 0;
END
