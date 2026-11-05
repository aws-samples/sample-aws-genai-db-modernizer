-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `repoint_replica`()
BEGIN
  STOP SLAVE;
  CHANGE MASTER TO MASTER_HOST = 'replica-source.example', MASTER_PORT = 3306;
  START SLAVE;
END
