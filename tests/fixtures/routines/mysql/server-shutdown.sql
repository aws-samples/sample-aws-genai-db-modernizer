-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `maintenance_stop`()
BEGIN
  SHUTDOWN;
END
