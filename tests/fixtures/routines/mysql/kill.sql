-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `end_idle_sessions`(IN p_id BIGINT)
BEGIN
  KILL p_id;
END
