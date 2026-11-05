-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` FUNCTION `read_report`() RETURNS text CHARSET utf8mb4
    READS SQL DATA
RETURN LOAD_FILE('/var/lib/mysql-files/report.txt')
