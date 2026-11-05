-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` FUNCTION `pad_code`(p_code CHAR(8)) RETURNS char(8) CHARSET utf8mb4
    NO SQL
    DETERMINISTIC
RETURN p_code
