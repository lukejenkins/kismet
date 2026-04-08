(
    typeof define === "function" ? function (m) { define("kismet-ui-cell", m); } :
    typeof exports === "object" ? function (m) { module.exports = m(); } :
    function(m) { }
)(function() {

"use strict";

var exports = {};
exports.load_complete = false;

kismet_ui.AddDeviceDetail("cell", "Cell Info", 10, {
    filter: function(data) {
        return (data["cellular.device"] != undefined && data["cellular.device"] != 0);
    },
    draw: function(data, target) {
        target.devicedata(data, {
            id: "cellData",
            fields: [
            {
                field: "cellular.device/cellular.cell.rat",
                title: "RAT",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.key",
                title: "Cell Key",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.operator",
                title: "Operator",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.mcc",
                title: "MCC",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.mnc",
                title: "MNC",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.tac",
                title: "TAC",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.cellid",
                title: "Cell ID",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.pci",
                title: "PCI",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.arfcn",
                title: "ARFCN",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.band",
                title: "Band",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.bandwidth",
                title: "Bandwidth",
                empty: "<i>Unknown</i>",
                render: function(opts) {
                    return opts['value'] + " MHz";
                },
            },
            {
                field: "cellular.device/cellular.cell.duplex",
                title: "Duplex",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.rsrp",
                title: "RSRP",
                empty: "<i>Unknown</i>",
                render: function(opts) {
                    var val = opts['value'];
                    var color = "#ff0000";
                    if (val > -80)
                        color = "#00ff00";
                    else if (val > -100)
                        color = "#ffff00";
                    return '<span style="color: ' + color + ';">' + val + ' dBm</span>';
                },
            },
            {
                field: "cellular.device/cellular.cell.rsrq",
                title: "RSRQ",
                empty: "<i>Unknown</i>",
                render: function(opts) {
                    return opts['value'] + " dB";
                },
            },
            {
                field: "cellular.device/cellular.cell.sinr",
                title: "SINR",
                empty: "<i>Unknown</i>",
                render: function(opts) {
                    return opts['value'] + " dB";
                },
            },
            {
                field: "cellular.device/cellular.cell.min_rsrp",
                title: "Min RSRP",
                empty: "<i>Unknown</i>",
                render: function(opts) {
                    return opts['value'] + " dBm";
                },
            },
            {
                field: "cellular.device/cellular.cell.max_rsrp",
                title: "Max RSRP",
                empty: "<i>Unknown</i>",
                render: function(opts) {
                    return opts['value'] + " dBm";
                },
            },
            {
                field: "cellular.device/cellular.cell.observation_count",
                title: "Observations",
                empty: "<i>Unknown</i>",
            },
            {
                field: "cellular.device/cellular.cell.seen_serving",
                title: "Seen as Serving",
                empty: "<i>Unknown</i>",
                render: function(opts) {
                    return opts['value'] ? "Yes" : "No";
                },
            },
            {
                field: "cellular.device/cellular.cell.seen_neighbor",
                title: "Seen via Observation",
                empty: "<i>Unknown</i>",
                render: function(opts) {
                    return opts['value'] ? "Yes" : "No";
                },
            },
            ],
        });
    },
});

/* Override the MAC address column for cell devices — show Cell Key instead,
 * since cell towers don't have MAC addresses (ours are synthetic hashes). */
kismet_ui.AddDeviceColumn("macaddr", {
    'title': 'Address',
    'description': 'Device address (MAC or Cell Key)',
    'field': 'kismet.device.base.macaddr',
    'sortable': true,
    'searchable': true,
    'render': (data, row, cell, onrender, aux) => {
        if (row != undefined && row['kismet.device.base.phyname'] === 'Cell') {
            var cd = row['cellular.device'];
            if (cd != undefined && cd['cellular.cell.key'] != undefined)
                return cd['cellular.cell.key'];
        }
        return kismet.censorMAC(data);
    },
});

kismet_ui.AddDeviceColumn("column_cell_rat", {
    sTitle: "Cell RAT",
    field: "cellular.device/cellular.cell.rat",
    description: "Cell Radio Access Technology",
    width: "6em",
    renderfunc: function(d, t, r, m) { return (d === "" || d === 0) ? "" : d; },
    visibility: "hidden",
});

kismet_ui.AddDeviceColumn("column_cell_key", {
    sTitle: "Cell Key",
    field: "cellular.device/cellular.cell.key",
    description: "Cell Tower ID (MCC/MNC_TAC_CellID)",
    width: "14em",
    renderfunc: function(d, t, r, m) { return (d === "" || d === 0) ? "" : d; },
    visibility: "hidden",
});

exports.load_complete = true;
return exports;

});
