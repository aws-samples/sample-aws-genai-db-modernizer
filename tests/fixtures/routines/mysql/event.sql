-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` EVENT `purge_logs` ON SCHEDULE EVERY 1 DAY STARTS '2026-01-01 00:00:00' ON COMPLETION PRESERVE ENABLE COMMENT 'nightly; DO not edit' DO BEGIN
  DELETE FROM app_logs WHERE created_at < NOW() - INTERVAL 30 DAY;
  REPEAT
    DELETE FROM app_log_archive WHERE created_at < NOW() - INTERVAL 1 YEAR LIMIT 1000;
  UNTIL ROW_COUNT() = 0 END REPEAT;
END
