-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `retune_literal`()
BEGIN
  PREPARE st FROM 'SET GLOBAL max_connections = 10';
  EXECUTE st;
  DEALLOCATE PREPARE st;
END
