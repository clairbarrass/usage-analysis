#!/usr//bin/env python
# scua.py (Slurm Code Usage Analysis)
#
# usage: scua.py [options] filename
#
# Compute software usage data from Slurm output.
# 
# positional arguments:
#  filename         Data file containing listing of Slurm jobs
#
# optional arguments:
#   -h, --help       show list of arguments
#
# The data file is the output for Slurm sacct, produced in using a 
# command such as:
#   sacct --format JobIDRaw,JobName%30,Account,NNodes,NTasks,ElapsedRaw,State,ConsumedEnergyRaw,MaxRSS,AveRSS -P --delimiter :: \
#        | egrep '[0-9]\.[0-9]' | egrep -v "RUNNING|PENDING|REQUEUED"
#
# The egrep extracts all subjobs and then excludes specific job states
#
#----------------------------------------------------------------------
# Copyright 2021, 2022 EPCC, The University of Edinburgh
#
# This file is part of usage-analysis.
#
# usage-analysis is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# usage-analysis is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with usage-analysis.  If not, see <http://www.gnu.org/licenses/>.
#----------------------------------------------------------------------
#
#

import numpy as np
import pandas as pd
import sys
import os
import re
import fnmatch
import argparse
import csv
from matplotlib import pyplot as plt
import seaborn as sns
from code_def import CodeDef

pd.options.mode.chained_assignment = None 

#=======================================================
# Functions to compute statistics
#=======================================================
# Get overall statistics in a dataframe
def getoverallstats(df, cu, en):
    totcu = df['Nodeh'].sum()
    percentcu = 100 * totcu / cu
    toten = df['Energy'].sum()
    percenten = 100 * toten / en
    totjobs = df['Count'].sum()
    return totcu, percentcu, toten, percenten, totjobs

# Get unweighted distribution
def getdist(df, col):
    minjob = df[col].min()
    maxjob = df[col].max()
    q1job = df[col].quantile(0.25)
    medjob = df[col].quantile(0.5)
    q3job = df[col].quantile(0.75)
    return minjob, maxjob, q1job, medjob, q3job

# Get weighted distribution
def getweighteddist(df, values, weights):
    df.sort_values(values, inplace=True)
    cumsum = df[weights].cumsum()
    cutoff = df[weights].sum() * 0.5
    median = float(df[cumsum >= cutoff][values].iloc[0])
    cutoff = df[weights].sum() * 0.25
    q1 = float(df[cumsum >= cutoff][values].iloc[0])
    cutoff = df[weights].sum() * 0.75
    q3 = float(df[cumsum >= cutoff][values].iloc[0])
    return q1, median, q3

# Get distribution statistics
#     Returns two lists, one with unweighted distribution, one with weighted distribution
def distribution(df, label, allcu, allen, values, weights):
    totcu, percentcu, toten, percenten, totjobs = getoverallstats(df, allcu, allen)
    minval, maxval, q1val, medval, q3val = getdist(df, values)
    dist = [label, minval, q1val, medval, q3val, maxval, totjobs, totcu, percentcu, toten, percenten]
    wq1val, wmedval, wq3val = getweighteddist(df, values, weights)
    wdist = [label, minval, wq1val, wmedval, wq3val, maxval, totjobs, totcu, percentcu, toten, percenten]
    return dist, wdist

# Get power distribution weighted by CU (nodeh)
def getweighteddist_power(df):
    df.sort_values('NodePower', inplace=True)
    cumsum = df.Nodeh.cumsum()
    cutoff = df.Nodeh.sum() * 0.5
    meduse = float(df.NodePower[cumsum >= cutoff].iloc[0])
    cutoff = df.Nodeh.sum() * 0.25
    q1use = float(df.NodePower[cumsum >= cutoff].iloc[0])
    cutoff = df.Nodeh.sum() * 0.75
    q3use = float(df.NodePower[cumsum >= cutoff].iloc[0])
    return meduse, q1use, q3use

# Compute version of dataframe weighted by use
def reindex_df(df, weight_col):
    """expand the dataframe to prepare for resampling
    result is 1 row per count per sample"""
    df = df.reindex(df.index.repeat(df[weight_col]))
    df.reset_index(drop=True, inplace=True)
    return(df)

#=======================================================
# Main code
#=======================================================

# Constants
CPN = 128          # Number of cores per node
MAX_POWER = 850    # Maximum per-node power draw to consider (in W) 

# Parse command line arguments
parser = argparse.ArgumentParser(description='Compute software usage data from Slurm output.')
parser.add_argument('filename', type=str, nargs=1, help='Data file containing listing of Slurm jobs')
parser.add_argument('--plots', dest='makeplots', action='store_true', default=False, help='Produce data plots')
parser.add_argument('--csv', dest='savecsv', action='store_true', default=False, help='Produce data plots')
parser.add_argument('--web', dest='webdata', action='store_true', default=False, help='Produce web data')
parser.add_argument('--power', dest='analysepower', action='store_true', default=False, help='Produce node power usage distribution')
parser.add_argument('--motif', dest='analysemotif', action='store_true', default=False, help='Produce algorithmic motif usage distribution')
parser.add_argument('--dropnan', dest='dropnan', action='store_true', default=False, help='Drop all rows that contain NaN. Useful for strict comparisons between usage and energy use.')
parser.add_argument('--prefix', dest='prefix', type=str, action='store', default='scua', help='Set the prefix to be used for output files')
parser.add_argument('--projects', dest='projlist', type=str, action='store', default=None, help='The file containing a list of project IDs and associated research areas')
parser.add_argument('-A', dest='account', type=str, action='store', nargs='?', default='', help='The slurm account specified for the report')
parser.add_argument('-u', dest='user', type=str, action='store', nargs='?', default='', help='The user specified for the report')
args = parser.parse_args()

# Read any code definitions
codeConfigDir = os.getenv('SCUA_BASE') + '/app-data/code-definitions'
codes = []
nCode = 0
codeDict = {}
codelist = []
for file in os.listdir(codeConfigDir):
    if fnmatch.fnmatch(file, '*.code'):
        nCode += 1
        code = CodeDef()   
        code.readConfig(codeConfigDir + '/' + file)
        codes.append(code)
        codeDict[code.name] = nCode - 1
        codelist.append(code.name)

# Read project research areas if required
areadict = {}
arealist = []
if args.projlist is not None:
    projfile = open(args.projlist, 'r')
    next(projfile)  # Skip header
    reader = csv.reader(projfile, skipinitialspace=True)
    areadict  = dict(reader)
    areaset = set(areadict.values()) # Unique area names

# Read dataset (usually saved from Slurm)
colid = ['JobID','ExeName','Account','Nodes','NTasks','Runtime','State','Energy','MaxRSS','MeanRSS']
df = pd.read_csv(args.filename[0], names=colid, sep='::', engine='python')
# Count helps with number of jobs
df['Count'] = 1

# Convert energy to numeric type
df['Energy'] = pd.to_numeric(df['Energy'], errors='coerce')
# Remove unrealistic values (presumably due to counter errors)
df['Energy'].mask(df['Energy'].gt(1e16), inplace=True)
df['NodePower'] = df['Energy'] / (df['Runtime'] * df['Nodes'])
# df.replace(np.inf, np.nan, inplace=True)
# Convert the energy to kWh
df['Energy'] = df['Energy'] / 3600000.0
# Check if we are dropping rows without energy values
if args.dropnan:
    df.dropna(axis=0, how='any', inplace=True)

# This section is to get the number of used cores, we need to make sure we catch
# jobs where people are using SMT and do not count the size of these wrong
df['cpn'] = df['NTasks'] / df['Nodes']
# Default is that NTasks actually corresponds to cores
df['Cores'] = df['NTasks']
# Catch those cases where jobs are using SMT and recompute core count
df.loc[df['cpn'] > CPN, 'Cores'] = df['Nodes'] * CPN

# Calculate the number of Nodeh
#   If the number of cores is less than a node then we need to get a 
#   fractional node hour count and fractional energy
#   Note: energy from Slurm is taken from node-level counters so if there 
#     are multiple job steps per node they are all assigned the full node
#     energy
#   Note: the weakness here is if people are using less cores than a full
#     node but are still using SMT. We will overcount the time for this case.
df['Nodeh'] = df['Nodes'] * df['Runtime'] / 3600.0
df.loc[df['Cores'] < CPN, 'Nodeh'] = df['Cores'] * df['Runtime'] / (CPN * 3600.0)
df.loc[df['Cores'] < CPN, 'Energy'] = df['Cores'] * df['Energy'] / CPN 
df.loc[df['Cores'] < CPN, 'NodePower'] = df['Cores'] * df['NodePower'] / CPN

# Remove very unrealistic values (presumably due to counter errors or very short runtimes)
df.replace(np.inf, np.nan, inplace=True)
df['NodePower'].mask(df['NodePower'].gt(MAX_POWER), inplace=True)

# Split JobID column into JobID and subjob ID
df['JobID'] = df['JobID'].astype(str)
df[['JobID','SubJobID']] = df['JobID'].str.split('.', 1, expand=True)

# Split Account column into ProjectID and GroupID
df['JobID'] = df['JobID'].astype(str)
df[['ProjectID','GroupID']] = df['Account'].str.split('-', 1, expand=True)

# Identify the codes using regex from the code definitions
df["Software"] = None
df["Motif"] = 'Unknown'
for code in codes:
    codere = re.compile(code.regexp)
    df.loc[df.ExeName.str.contains(codere), ["Software", "Motif"]] = [code.name, code.type]

# Get the list of motifs from the column (get unique words in the column)
motif_set = set()
df['Motif'].str.split().apply(motif_set.update)

# Add research areas if supplied
if args.projlist is not None:
    df['Area'] = df['ProjectID'].map(areadict)

# Restrict to specified project code if requested
if args.account:
    df.drop(df[df.ProjectID != args.account].index, inplace=True)
    print(df)

if args.makeplots:
    plt.figure(figsize=[6,2])
    sns.boxplot(
        x="Cores",
        orient='h',
        color='lightseagreen',
        showmeans=True,
        width=0.25,
        meanprops={
            "marker":"o",
            "markerfacecolor":"white",
            "markeredgecolor":"black",
            "markersize":"5"
            },
        data=reindex_df(df, weight_col='Nodeh')
        )
    plt.xlabel('Cores')
    sns.despine()
    plt.tight_layout()
    plt.savefig(f'{args.prefix}_overall_boxplot.png', dpi=300)
    plt.clf()

print("\n----------------------------------")
print("# SCUA (Slurm Code Usage Analysis)")
print()
print("EPCC, 2021, 2022")
print("----------------------------------\n")

if not args.user is None and args.user != "":
    print("On user account " + args.user + "\n");
if not args.account is None and args.account != "":
    print("On slurm account " + args.account + "\n");
if args.dropnan:
    print("Dropping job steps with no energy values recorded\n");

# Software usage plots
if args.makeplots:
    # Bar plot of software usage
    df_plot = df_usage[~df_usage['Software'].isin(['Overall','Unidentified'])]
    plt.figure(figsize=[8,6])
    sns.barplot(y='Software', x='PercentUse', color='lightseagreen', data=df_plot)
    sns.despine()
    plt.xlabel('PercentUse')
    plt.tight_layout()
    plt.savefig(f'{args.prefix}_codes_usage.png', dpi=300)
    plt.clf()
    # Boxplots for top 15 software by CU use
    topcodes = df_usage['Software'].head(16).to_list()[1:]
    df_topcodes = df[df['Software'].isin(topcodes)]
    plt.figure(figsize=[8,6])
    sns.boxplot(
        y="Software",
        x="Cores",
        orient='h',
        color='lightseagreen',
        order=topcodes,
        showmeans=True,
        meanprops={
            "marker":"o",
            "markerfacecolor":"white",
            "markeredgecolor":"black",
            "markersize":"5"
            },
        data=reindex_df(df_topcodes, weight_col='Nodeh')
        )
    plt.xlabel('Cores')
    sns.despine()
    plt.tight_layout()
    plt.savefig(f'{args.prefix}_top15_boxplot.png', dpi=300)
    plt.clf()

######################################################################
# Data quality checks
#

allcu = df['Nodeh'].sum()
allen = df['Energy'].sum()

# Software that are unidentified but have more than 1% of total use
mask = df['Software'].values == None
df_code = df[mask]
groupf = {'Nodeh':'sum', 'Count':'sum'}
df_group = df_code.groupby(['ExeName']).agg(groupf)
df_group.sort_values('Nodeh', inplace=True, ascending=False)
thresh = allcu * 0.01
print('\n## Unidentified executables with significant use\n')
print(df_group.loc[df_group['Nodeh'] >= thresh].to_markdown(floatfmt=".1f"))
print()

# Check quality of energy data
#
nNaN = df['Energy'].isna().sum()
NaNUsage = df.loc[df['Energy'].isna(), 'Nodeh'].sum()
nRow = df.shape[0]
print('\n## Energy data quality check\n')
print(f'{"Number of subjobs =":>30s} {nRow:>10d}')
print(f'{"Subjobs missing energy =":>30s} {nNaN:>10d} ({100*nNaN/nRow:.2f}%)')
print(f'{"Usage missing energy =":>30s} {NaNUsage:>10.1f} Nodeh ({100*NaNUsage/allcu:.2f}%)\n')

# Check quality of node power data
#
nsmall = df['Cores'].lt(CPN).sum()
coresusage = df.loc[df['Cores'].lt(CPN), 'Nodeh'].sum()
energyusage = df.loc[df['Cores'].lt(CPN), 'Energy'].sum()
nRow = df.shape[0]
print('\n## Node power data quality check\n')
print(f'{"Number of subjobs =":>30s} {nRow:>10d}')
print(f'{"Subjobs excluded =":>30s} {nsmall:>10d} ({100*nsmall/nRow:.2f}%)')
print(f'{"Usage excluded =":>30s} {coresusage:>10.1f} Nodeh ({100*coresusage/allcu:.2f}%)')
print(f'{"Energy excluded =":>30s} {energyusage:>10.1f} Nodeh ({100*energyusage/allen:.2f}%)\n')

######################################################################
# Define the distribution analyses to perform
#
outputs = []
title = {}
category_list = {}
category_col = {}
analysis_col = {}
analyse_none = {}
full_node = {}

# Job size distribution for software is always computed
category = 'CodeSize'
outputs.append(category)
title[category] = 'Software job size'
category_list[category] = codelist
category_col[category] = 'Software'
analysis_col[category] = 'Cores'
analyse_none[category] = True
full_node[category] = False
# Optional categories
if args.analysepower:
    category = 'CodePower'
    title[category] = 'Software node power use'
    outputs.append(category)
    category_list[category] = codelist
    category_col[category] = 'Software'
    analysis_col[category] = 'NodePower'
    analyse_none[category] = True
    full_node[category] = True
if args.projlist is not None:
    category = 'AreaSize'
    outputs.append(category)
    title[category] = 'Research area job size'
    category_list[category] = areaset
    category_col[category] = 'Area'
    analysis_col[category] = 'Cores'
    analyse_none[category] = False
    full_node[category] = False
if args.analysepower and args.projlist is not None:
    category = 'AreaPower'
    title[category] = 'Research area node power use'
    outputs.append(category)
    category_list[category] = areaset
    category_col[category] = 'Area'
    analysis_col[category] = 'NodePower'
    analyse_none[category] = False
    full_node[category] = True
if args.analysemotif:
    category = 'MotifSize'
    title[category] = 'Algorithmic motifs job size'
    outputs.append(category)
    category_list[category] = motif_set
    category_col[category] = 'Motif'
    analysis_col[category] = 'Cores'
    analyse_none[category] = False
    full_node[category] = False
if args.analysemotif and args.analysepower:
    category = 'MotifPower'
    title[category] = 'Algorithmic motifs node power use'
    outputs.append(category)
    category_list[category] = motif_set
    category_col[category] = 'Motif'
    analysis_col[category] = 'NodePower'
    analyse_none[category] = False
    full_node[category] = True

######################################################################
# Loop over the defined output categories computing distributions
# and outputting the results
#
for output in outputs:
    df_output = df
    # This restricts to full nodes if needed
    if full_node[output]:
        mask = df['Cores'].ge(CPN)
        df_output = df[mask]

    job_stats = []
    usage_stats = []
    categories = category_list[output]
    # The category column and analysis columns
    catcol = category_col[output]
    ancol = analysis_col[output]
    for category in categories:
        mask = df_output[catcol].str.contains(re.escape(category), na=False)
        df_cat = df_output[mask]
        if not df_cat.empty:
            dist, wdist = distribution(df_cat, category, allcu, allen, ancol, 'Nodeh')
            job_stats.append(dist)
            usage_stats.append(wdist)

    # Get the data for unidentified (if needed)
    if analyse_none[output]:
        mask = df_output[catcol].values == None
        df_cat = df_output[mask]
        if not df_cat.empty:
            dist, wdist = distribution(df_cat, 'Unidentified', allcu, allen, ancol, 'Nodeh')
            job_stats.append(dist)
            usage_stats.append(wdist)

    # Get overall data for all jobs
    dist, wdist = distribution(df_output, 'Overall', allcu, allen, ancol, 'Nodeh')
    job_stats.append(dist)
    usage_stats.append(wdist)

    # Save the output
    print(f'\n## {title[output]} distribution: weighted by usage\n')
    df_usage = pd.DataFrame(usage_stats, columns=[catcol, 'Min', 'Q1', 'Median', 'Q3', 'Max', 'Jobs', 'Nodeh', 'PercentUse', 'kWh', 'PercentEnergy'])
    if args.webdata:
        df_usage.drop('Nodeh', axis=1, inplace=True)
        df_usage.drop('kWh', axis=1, inplace=True)
    df_usage.sort_values('PercentUse', inplace=True, ascending=False)
    print(df_usage.to_markdown(index=False, floatfmt=".1f"))
    print()
    print(f'\n## {title[output]} distribution: by number of jobs\n')
    df_jobs = pd.DataFrame(job_stats, columns=[catcol, 'Min', 'Q1', 'Median', 'Q3', 'Max', 'Jobs', 'Nodeh', 'PercentUse', 'kWh', 'PercentEnergy'])
    if args.webdata:
        df_jobs.drop('Nodeh', axis=1, inplace=True)
        df_jobs.drop('kWh', axis=1, inplace=True)
    df_jobs.sort_values('PercentUse', inplace=True, ascending=False)
    print(df_jobs.to_markdown(index=False, floatfmt=".1f"))
    print()
    # Save as CSV if required
    if args.savecsv:
        df_jobs.to_csv(f'{args.prefix}_{output}.csv', index=False, float_format="%.1f")
        df_usage.to_csv(f'{args.prefix}_{output}_weighted.csv', index=False, float_format="%.1f")


