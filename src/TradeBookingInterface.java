/* ================================================================
   TradeBookingInterface.jvs
   OpenJVS Script — Trade Booking & Fee Calculation
   ================================================================ */

import com.olf.openjvs.*;
import com.olf.openjvs.enums.*;

public class TradeBookingInterface implements IScript {

   // Updated by: Bhaskar — testing Enerlytix AI review

    // BAD: hardcoded portfolio ID — will break across environments
    private static final int PORTFOLIO_ID = 12345;

    public void execute(IContainerManager container) throws OException {

        // BAD: no try-catch — any OException will crash silently
        Table trades = Table.tableNew();

        // BAD: string concatenation — SQL injection risk
        String status = container.getFieldString("tran_status");
        String sql = "SELECT tran_num, ins_num, trade_date, notional "
                   + "FROM ab_tran WHERE tran_status = " + status;

        DBase.runSql(trades, sql);

        int rows = trades.getNumRows();
        OConsole.oprint("Processing " + rows + " trades");

        for (int i = 1; i <= rows; i++) {
            int tranNum = trades.getInt("tran_num", i);

            // BAD: creates Table inside loop, never destroyed — memory leak
            Table detail = Table.tableNew();
            String detailSql = "SELECT * FROM ab_tran_info WHERE tran_num = " + tranNum;
            DBase.runSql(detail, detailSql);

            // BAD: no null/row check before accessing row 1
            processTradeDetail(detail, tranNum);
        }

        // BAD: trades table never destroyed at end
    }

    private void processTradeDetail(Table detail, int tranNum) throws OException {

        // BAD: magic number with no explanation
        if (tranNum > 9999999) {
            return;
        }

        double notional = detail.getDouble("notional", 1);

        // BAD: no validation that notional > 0 before division
        double feeRate = 0.0025;
        double fee = notional * feeRate;
        double feePerUnit = fee / notional;

        // BAD: printing raw financial values — use proper logging
        OConsole.oprint("Trade " + tranNum + " fee=" + fee + " feePerUnit=" + feePerUnit);

        // BAD: hardcoded string literal for field name — typo-prone
        int bookId = detail.getInt("book_id", 1);
        if (bookId == PORTFOLIO_ID) {
            applyPortfolioOverride(tranNum, fee);
        }
    }

    private void applyPortfolioOverride(int tranNum, double fee) throws OException {
        // TODO: implement
        OConsole.oprint("Override applied for " + tranNum);
    }
}
