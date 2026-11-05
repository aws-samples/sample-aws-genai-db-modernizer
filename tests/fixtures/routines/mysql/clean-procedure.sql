-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `close_order`(IN p_order_id INT, OUT p_total DECIMAL(10,2))
    MODIFIES SQL DATA
    SQL SECURITY DEFINER
    COMMENT 'Closes one order; the comment holds a ; and an END'
main: BEGIN
  DECLARE v_status VARCHAR(16) DEFAULT 'open';
  DECLARE v_done INT DEFAULT 0;
  DECLARE no_row CONDITION FOR SQLSTATE '02000';
  DECLARE c_lines CURSOR FOR SELECT l.amount FROM order_lines l WHERE l.order_id = p_order_id;
  DECLARE CONTINUE HANDLER FOR no_row SET v_done = 1;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION, 1062
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;
  SELECT o.status, o.total INTO v_status, p_total FROM orders o WHERE o.id = p_order_id FOR UPDATE;
  IF v_status = 'closed' THEN
    LEAVE main;
  ELSEIF v_status IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'no such order; END IF';
  ELSE
    SET p_total = (SELECT SUM(amount) FROM order_lines WHERE order_id = p_order_id);
  END IF;
  CASE v_status
    WHEN 'open' THEN UPDATE orders SET status = 'closed', total = CASE WHEN p_total < 0 THEN 0 ELSE p_total END WHERE id = p_order_id;
    ELSE BEGIN END;
  END CASE;
  scan: LOOP
    IF v_done = 1 THEN LEAVE scan; END IF;
    ITERATE scan;
  END LOOP scan;
  CALL audit.log_event('close', p_order_id);
  INSERT INTO order_history (order_id, total) SELECT id, total FROM orders WHERE id = order_tax(p_order_id);
END main
