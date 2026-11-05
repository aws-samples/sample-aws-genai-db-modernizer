-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `retune_variable`()
BEGIN
  SET @stmt = 'SET GLOBAL max_connections = 10';
  PREPARE st FROM @stmt;
  EXECUTE st;
  DEALLOCATE PREPARE st;
END
